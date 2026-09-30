# -*- coding: utf-8 -*-
"""RotateCenter 视口可视化（GPU overlay）。

背景
----
``RotateCenter``（也叫 ``RotateShaft``）是 ``RotateAxis`` rig 的**静止
旋转中心** —— runtime 绕它的世界 AABB 中心旋转 rotator，但运行时**不会
写它的 rotation_euler**（避免与场景层级冲突）。这导致美术在 3D 视口
里看不出：

1. **哪个物体是旋转中心** —— 场景里可能有一堆 mesh，"哪个是 center
   哪个是 rotator"很难一眼看出来；
2. **rotation_axis_index 对应的局部轴是哪一条** —— 0/1/2 是局部 X/Y/Z，
   但中心被旋转 / 摆斜后局部轴在世界的指向对不上直觉；
3. **旋转中心的精确世界位置** —— runtime 算的是中心 mesh 的世界 AABB
   中点，不是它的 origin；两者在长方体 / 斜放中心上会差几个 BU。

绘制两类对象
------------
- **A. 已配置的 RotateCenter mesh**（名字在 ``("RotateCenter",
  "RotateShaft")`` 且父级以 ``RotateAxis`` 开头）：用 mesh 的**世界 AABB
  中心**作为 pivot，画 3 条局部轴 + 当前轴强调线 + 十字标。
- **B. RotateAxis host Empty**（名字以 ``RotateAxis`` 开头）：用作**回退
  标记**。当美术还没建 RotateCenter mesh 时，至少能在 host origin 位置
  看到一个琥珀色十字 + 局部轴，让用户知道"轴 rig 的 host 在这 / 局部
  朝向是这样"。已经画过 A 类的 host 不重复画 B 类。

绘制内容（A、B 通用）
--------------------
- **3 条局部坐标轴**（红 X / 绿 Y / 蓝 Z），从 pivot 出发，长度随 mesh
  最大局部维度或固定 0.2 BU 缩放；
- **当前 rotate_axis_index 强调线**：**白色更长的线段**，明确指出
  ``rotate_axis_index`` 对应的轴；
- **旋转中心十字标**：以 pivot 为圆心的 6 向小十字（每轴 ±5% 长度），
  琥珀色。

GPU 兼容性
----------
Blender 5.x 用 ``UNIFORM_COLOR`` shader +
``SpaceView3D.draw_handler_add(callback, args, "WINDOW", "POST_VIEW")``。
代码直接喂世界坐标，draw handler 自动绑视图矩阵。

开关 & 调试
-----------
- scene[``ms_show_rotate_center_overlay``]：总开关（默认 True）。
- :func:`get_last_counts`：返回 ``(center_count, host_count)``，可在
  Dev Panel 或 Python 控制台调用来确认 overlay 是否真的在画。
"""

from __future__ import annotations

import traceback

import bpy

try:
    from .naming import is_host as _is_rotate_host
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.RotateAxis.naming import is_host as _is_rotate_host  # noqa: F401


# scene 自定义属性键 —— 跟 sensor overlay 的命名风格一致
SCENE_SHOW_KEY = "ms_show_rotate_center_overlay"
# 调试：上一帧画了多少对象（A 类 + B 类）
_LAST_COUNTS = (0, 0)


# 局部轴颜色（X 红 / Y 绿 / Z 蓝），强调线、十字标颜色
_COLOR_X = (0.95, 0.25, 0.25, 1.0)
_COLOR_Y = (0.30, 0.95, 0.30, 1.0)
_COLOR_Z = (0.35, 0.60, 1.00, 1.0)
_COLOR_AXIS = (1.0, 1.0, 1.0, 1.0)      # 当前 rotate_axis_index 强调线
_COLOR_PIVOT = (1.0, 0.85, 0.20, 1.0)    # 旋转中心十字标
_COLOR_HOST = (0.55, 0.30, 0.95, 1.0)    # host 回退标记（紫色）

# 模块状态：draw handler 句柄 + 错误日志（远端排查用）
_HANDLE = None
_ERRORS = []


# ---------------------------------------------------------------- 判定


def _is_rotate_center_mesh(obj) -> bool:
    """判定一个 Blender 对象是不是某个 ``RotateAxis*`` host 的旋转中心。

    彻底改为读 cfg：遍历 scene 里所有 ``RotateAxis*`` host，看 ``obj``
    是否被某个 host 的 ``cfg.rotate_center`` 引用。这是 ground truth
    —— runtime 也是走这条路径，不再靠 "RotateCenter" / "Shaft" /
    "RotateShaft" 等命名匹配。美术改名 / 复制 / 用不同前缀都能正常
    画出，只要 panel 里关联了 pointer。

    注意：这条逻辑仍然只在 ``MESH`` 类型上画（Empty 没 bound_box，画不出
    pivot 十字）。隐藏物体也跳过。
    """
    if obj is None:
        return False
    if getattr(obj, "type", None) != "MESH":
        return False
    if getattr(obj, "hide_viewport", False):
        return False
    scene = bpy.context.scene
    if scene is None:
        return False
    for host in scene.objects:
        if not _is_rotate_host(host):
            continue
        cfg = getattr(host, "rotate_axis", None)
        if cfg is None:
            continue
        if getattr(cfg, "rotate_center", None) is obj:
            return True
    return False


def _is_rotate_axis_host(obj) -> bool:
    """判定一个对象是不是 ``RotateAxis*`` Empty host。

    委托给 :func:`naming.is_host` —— 已用 ``parse_host_id`` 重写，兼容
    ``RotateAxis1`` / ``RotateAxis_2`` / ``RotateAxis.001`` / ``RotateAxis-Turret``
    等所有非空前缀。
    """
    if obj is None:
        return False
    if getattr(obj, "hide_viewport", False):
        return False
    return bool(_is_rotate_host(obj))


def _read_axis_index(obj) -> int:
    """读 ``rotate_axis_index``（0=X / 1=Y / 2=Z）。默认 2（Z）。"""
    try:
        value = getattr(obj, "rotate_axis_index", 2)
        idx = int(value)
        return idx if idx in (0, 1, 2) else 2
    except Exception:
        return 2


def _local_bb(obj):
    """obj 局部坐标系的包围盒 ``(mn, mx)``；拿不到返回 ``None``。"""
    bb = getattr(obj, "bound_box", None)
    if bb is None or len(bb) < 8:
        return None
    mn = [min(c[i] for c in bb) for i in range(3)]
    mx = [max(c[i] for c in bb) for i in range(3)]
    return mn, mx


# ---------------------------------------------------------------- 绘制主体


def _draw_target(pivot, m3, length, axis_index, is_host_fallback: bool,
                 gpu, batch_for_shader, shader) -> None:
    """绘制单个目标（A 类 mesh 或 B 类 host 回退）。

    ``pivot`` 是世界空间的 Vector；``m3`` 是该对象的 ``matrix_world.to_3x3()``；
    ``length`` 是局部轴长度（BU）；``axis_index`` 0/1/2 对应 X/Y/Z。
    """
    from mathutils import Vector  # type: ignore  # Blender 环境

    # ---- 1) 局部坐标轴（红 / 绿 / 蓝），从 pivot 出发 ----
    axes = (
        (_COLOR_X, Vector((1.0, 0.0, 0.0))),
        (_COLOR_Y, Vector((0.0, 1.0, 0.0))),
        (_COLOR_Z, Vector((0.0, 0.0, 1.0))),
    )
    for color, local_dir in axes:
        end = pivot + m3 @ (local_dir * length)
        pts = [(pivot.x, pivot.y, pivot.z), (end.x, end.y, end.z)]
        batch = batch_for_shader(shader, "LINES", {"pos": pts})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    # ---- 2) 当前 rotate_axis_index 强调线：更长更亮 ----
    dir_local = axes[axis_index][1]
    end_dir = pivot + m3 @ (dir_local * (length * 1.5))
    pts = [(pivot.x, pivot.y, pivot.z), (end_dir.x, end_dir.y, end_dir.z)]
    batch = batch_for_shader(shader, "LINES", {"pos": pts})
    shader.bind()
    shader.uniform_float("color", _COLOR_AXIS)
    batch.draw(shader)

    # ---- 3) 旋转中心十字标 ----
    arm = length * 0.15
    cross_pts = []
    for _color, local_dir in axes:
        for sign in (-1.0, 1.0):
            end = pivot + m3 @ (local_dir * arm * sign)
            cross_pts.append((pivot.x, pivot.y, pivot.z))
            cross_pts.append((end.x, end.y, end.z))
    batch = batch_for_shader(shader, "LINES", {"pos": cross_pts})
    gpu.state.line_width_set(2.0)
    shader.bind()
    # host 回退用紫色，真实 center 用琥珀色 —— 一眼区分"这是 fallback"
    shader.uniform_float("color", _COLOR_HOST if is_host_fallback else _COLOR_PIVOT)
    batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _draw_rotate_center_mesh(obj, gpu, batch_for_shader, shader) -> None:
    """A 类：已配置的 RotateCenter mesh（用世界 AABB 中心作 pivot）。"""
    mw = obj.matrix_world
    if mw is None:
        return
    bb = _local_bb(obj)
    if bb is None:
        return
    try:
        from mathutils import Vector  # type: ignore  # Blender 环境
    except ImportError:
        return
    mn, mx = bb
    ext = [mx[i] - mn[i] for i in range(3)]
    if max(ext) <= 1e-6:
        return
    length = max(ext) * 1.2
    pivot = mw @ Vector(((mn[0] + mx[0]) * 0.5,
                          (mn[1] + mx[1]) * 0.5,
                          (mn[2] + mx[2]) * 0.5))
    _draw_target(pivot, mw.to_3x3(), length,
                 _read_axis_index(obj), False,
                 gpu, batch_for_shader, shader)


def _draw_host_fallback(obj, gpu, batch_for_shader, shader) -> None:
    """B 类：RotateAxis host Empty（host origin 作 pivot，固定 0.2 BU 轴长）。"""
    mw = obj.matrix_world
    if mw is None:
        return
    length = 0.2  # host 是 Empty 没 bound_box，用固定长度
    pivot = mw.translation
    _draw_target(pivot, mw.to_3x3(), length,
                 _read_axis_index(obj), True,
                 gpu, batch_for_shader, shader)


# ---------------------------------------------------------------- draw handler


def _draw() -> None:
    """SpaceView3D draw handler 回调：遍历 scene 绘制 center mesh + host 回退。"""
    global _LAST_COUNTS
    center_count = 0
    host_count = 0
    try:
        scene = bpy.context.scene
        if scene is None:
            return
        try:
            show = bool(getattr(scene, SCENE_SHOW_KEY, True))
        except Exception:
            show = True
        if not show:
            _LAST_COUNTS = (0, 0)
            return
    except Exception:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception as exc:  # GPU 初始化失败等：静默跳过，不影响插件主体
        _ERRORS.append(f"gpu import: {exc}")
        return
    try:
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        # 先记录哪些 host 已经被 A 类（center mesh）覆盖 —— 这些 host 不画 B 类。
        covered_hosts: set = set()
        for obj in scene.objects:
            try:
                if _is_rotate_center_mesh(obj):
                    _draw_rotate_center_mesh(obj, gpu, batch_for_shader, shader)
                    center_count += 1
                    parent = getattr(obj, "parent", None)
                    if parent is not None:
                        covered_hosts.add(id(parent))
            except Exception:
                _ERRORS.append(
                    f"rotate_center mesh {getattr(obj, 'name', '?')}:\n"
                    + traceback.format_exc()
                )
                if len(_ERRORS) > 20:
                    _ERRORS.pop(0)
        for obj in scene.objects:
            try:
                if id(obj) in covered_hosts:
                    continue
                if _is_rotate_axis_host(obj):
                    _draw_host_fallback(obj, gpu, batch_for_shader, shader)
                    host_count += 1
            except Exception:
                _ERRORS.append(
                    f"rotate_axis host {getattr(obj, 'name', '?')}:\n"
                    + traceback.format_exc()
                )
                if len(_ERRORS) > 20:
                    _ERRORS.pop(0)
    except Exception:
        _ERRORS.append(traceback.format_exc())
        if len(_ERRORS) > 20:
            _ERRORS.pop(0)
    finally:
        _LAST_COUNTS = (center_count, host_count)


def get_last_counts() -> tuple:
    """返回上一帧画的对象数 ``(center_count, host_count)``。

    可在 Dev Panel、Python 控制台或 MCP 里调用来确认 overlay 是否在
    正常运行。如果返回 ``(0, 0)`` 而 scene 里有 RotateAxis host，说明：

    - draw handler 没注册（addon 没重启 / ``register()`` 抛错）；
    - 或 GPU 环境初始化失败（看 :data:`_ERRORS`）；
    - 或 scene 开关被关。
    """
    return _LAST_COUNTS


def get_errors() -> list:
    """返回最近一次绘制捕获的异常列表（最近 20 条）。"""
    return list(_ERRORS)


def is_registered() -> bool:
    """返回 draw handler 是否成功注册。False 通常意味着 ``register()``
    没跑过（addon disable → enable 没做），或者调用时抛错被吞了。"""
    return _HANDLE is not None


# ---------------------------------------------------------------- 生命周期


def register() -> None:
    """注册 scene 开关属性 + 视口 draw handler（幂等）。"""
    global _HANDLE
    if not hasattr(bpy.types.Scene, SCENE_SHOW_KEY):
        setattr(
            bpy.types.Scene, SCENE_SHOW_KEY,
            bpy.props.BoolProperty(name="Rotate Center Overlay", default=True),
        )
    if _HANDLE is None:
        # Blender 5.x 签名：draw_handler_add(callback, args, region_type, draw_type)
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), "WINDOW", "POST_VIEW"
        )


def unregister() -> None:
    """移除 draw handler / scene 属性（幂等）。"""
    global _HANDLE
    if _HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        except Exception:
            pass
        _HANDLE = None
    if hasattr(bpy.types.Scene, SCENE_SHOW_KEY):
        try:
            delattr(bpy.types.Scene, SCENE_SHOW_KEY)
        except Exception:
            pass


__all__ = [
    "SCENE_SHOW_KEY",
    "register",
    "unregister",
    "get_last_counts",
    "get_errors",
    "is_registered",
]