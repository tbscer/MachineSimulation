# -*- coding: utf-8 -*-
"""U 型传感器视口可视化（调试辅助）。

背景
----
``sensor_direction``（X / Y / Z）是按 sensor **自身局部坐标**解释的：
沿哪条局部轴切出“触发薄片”，薄片法向即该局部轴、位置在该轴方向上的
包围盒中点。这个语义对任意摆放的 sensor（包括斜 45°）都成立，但旋转过
的物体在视口里看不出它自己的局部 X / Y / Z 指向，用户很难确定该填哪个
方向。

本模块在 3D 视口为每个已配置的 U 型传感器叠加绘制：

- **局部坐标轴**（红 = 局部 X、绿 = 局部 Y、蓝 = 局部 Z），从物体原点
  出发，长度随传感器尺寸缩放 —— 用于辨认旋转后每个局部轴的世界指向；
- **当前选中的 direction 轴**用更长的白色线强调；
- **触发“面”**：方向 = direction，位于 sensor 沿该方向的中间位置的半透明
  平面（琥珀色），横向范围 = sensor 在另外两轴上的完整尺寸 —— 即用户定义
  的触发区：很薄的平面/薄块，位置在传感器沿法向的中间；
- **实际检测薄片线框**：沿方向厚度 = ``sensor_thickness`` 的真实触发体，
  改方向 / 厚度时即时变化。

GPU 兼容性
----------
Blender 5.x 移除了旧的 ``3D_UNIFORM_COLOR``，内置着色器改为
``UNIFORM_COLOR`` / ``POLYLINE_UNIFORM_COLOR``，二者都带内置
``ModelViewProjectionMatrix``（3D 视口 draw handler 会自动绑定视图矩阵），
所以直接喂世界坐标即可。``SpaceView3D.draw_handler_add`` 的签名也变成了
``(callback, args, region_type, draw_type)``，draw_type 用 ``POST_VIEW``。

开关
----
- scene[``ms_show_sensor_overlay``]：总开关（默认 True）。
- scene[``ms_sensor_overlay_live``]：是否用一个低频 timer（约 4 Hz）持续
  刷新视口，让调整方向/厚度时立刻可见（默认 True）。
"""

from __future__ import annotations

import traceback

import bpy

# scene 自定义属性键
SCENE_SHOW_KEY = "ms_show_sensor_overlay"
SCENE_LIVE_KEY = "ms_sensor_overlay_live"

# 局部轴颜色（X 红 / Y 绿 / Z 蓝）与触发体颜色
_COLOR_X = (0.95, 0.25, 0.25, 1.0)
_COLOR_Y = (0.30, 0.95, 0.30, 1.0)
_COLOR_Z = (0.35, 0.60, 1.00, 1.0)
_COLOR_DIR = (1.0, 1.0, 1.0, 1.0)    # 当前选中 direction 轴的强调线
_COLOR_BOX = (1.0, 0.62, 0.10, 1.0)   # 实际检测薄片线框
_COLOR_FILL = (1.0, 0.55, 0.12, 0.30)  # 中间触发“面”半透明填充

# 盒的 8 条棱（线框用）
_BOX_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)
_DIR_INDEX = {"X": 0, "Y": 1, "Z": 2}

# 模块状态：draw handler 句柄 + 刷新 timer 句柄 + 错误日志（便于远端排查）
_HANDLE = None
_TIMER = None
_ERRORS = []


# ---------------------------------------------------------------- 基础几何


def _read_direction(obj) -> int:
    """读 sensor_direction（EnumProperty 字符串或旧版 int 0/1/2）。"""
    try:
        value = obj.sensor_direction
        idx = _DIR_INDEX.get(value, 0)
        return idx if isinstance(idx, int) else 0
    except Exception:
        pass
    try:
        legacy = obj.get("sensor_direction", 0)
        if isinstance(legacy, (int, float)):
            return int(legacy) % 3
        if isinstance(legacy, str):
            return _DIR_INDEX.get(legacy, 0)
    except Exception:
        pass
    return 0


def _read_thickness(obj) -> float:
    """读触发厚度。兼容两种来源：旧版 custom key 优先，其次 RNA 属性
    ``Object.sensor_thickness``（host 面板里的可编辑输入）。"""
    try:
        value = obj.get("sensor_thickness", None)
        if value is not None:
            return max(float(value), 1e-9)
    except Exception:
        pass
    try:
        return max(float(getattr(obj, "sensor_thickness", 0.001)), 1e-9)
    except Exception:
        return 0.001


def _scene_bool(key: str, default: bool) -> bool:
    """读 scene 开关。面板勾选写入的是 RNA 属性（``bpy.types.Scene`` 注册的
    BoolProperty），必须用 ``getattr``；``scene.get`` 只能读 ID 自定义属性，
    用来读 RNA 属性会永远拿到默认值——这就是之前 overlay 关不掉的根因。"""
    scene = bpy.context.scene
    if scene is None:
        return default
    try:
        return bool(getattr(scene, key, default))
    except Exception:
        pass
    try:
        return bool(scene.get(key, default))
    except Exception:
        return default


def _local_bb(obj):
    """sensor 局部坐标系的包围盒 (mn, mx) 列表；拿不到就返回 None。"""
    bb = getattr(obj, "bound_box", None)
    if bb is None or len(bb) < 8:
        return None
    mn = [min(c[i] for c in bb) for i in range(3)]
    mx = [max(c[i] for c in bb) for i in range(3)]
    return mn, mx


def _detection_box_corners(mn, mx, d: int, thickness: float):
    """按 direction+thickness 生成触发薄片的 8 个 local 角点（含复制）。"""
    c = (mn[d] + mx[d]) * 0.5
    lo = list(mn)
    hi = list(mx)
    lo[d] = c - thickness * 0.5
    hi[d] = c + thickness * 0.5
    return [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]),
            (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
            (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]),
            (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2])]


def _is_sensor_obj(obj) -> bool:
    """判定一个物体是不是“被配置过的”U 型传感器。

    只读 ``sensor_type`` custom property（由 :class:`UTypeSensor` 在
    构建几何时写入）。不再用命名兜底：一旦美术在 panel 里把某个 mesh
    关联成 sensor pointer 并启动过一次 detection，就会有这个标记；新建
    还没关联的 sensor 自然不画，避免误绘其他几何体。
    """
    if obj is None or obj.type != "MESH":
        return False
    if getattr(obj, "hide_viewport", False):
        return False
    try:
        if obj.get("sensor_type") == "u_type":
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- 绘制主体


def _draw_sensor(obj, gpu, batch_for_shader) -> None:
    """绘制单个传感器：局部轴三色线 + 选中方向白线 + 触发盒。"""
    from mathutils import Matrix, Vector  # type: ignore  # Blender 环境

    mw = obj.matrix_world
    if mw is None:
        return
    m3 = mw.to_3x3()
    origin = mw.translation
    bb = _local_bb(obj)
    if bb is None:
        return
    mn, mx = bb
    ext = [mx[i] - mn[i] for i in range(3)]
    if max(ext) <= 1e-6:
        return
    d = _read_direction(obj)
    thickness = _read_thickness(obj)

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")

    # ---- 1) 局部坐标轴（红/绿/蓝），从物体原点出发 ----
    # 轴长取包围盒最长边的 ~0.9 倍，保证比触发盒本身大一圈、肉眼可辨。
    length = max(ext) * 0.9
    axes = (
        (_COLOR_X, Vector((1.0, 0.0, 0.0))),
        (_COLOR_Y, Vector((0.0, 1.0, 0.0))),
        (_COLOR_Z, Vector((0.0, 0.0, 1.0))),
    )
    for i, (color, local_dir) in enumerate(axes):
        end = origin + m3 @ (local_dir * length)
        pts = [(origin.x, origin.y, origin.z), (end.x, end.y, end.z)]
        batch = batch_for_shader(shader, "LINES", {"pos": pts})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    # ---- 2) 当前选中的 direction 轴：更长更亮的白线 ----
    dir_local = axes[d][1]
    end_dir = origin + m3 @ (dir_local * (length * 1.35))
    pts = [(origin.x, origin.y, origin.z), (end_dir.x, end_dir.y, end_dir.z)]
    batch = batch_for_shader(shader, "LINES", {"pos": pts})
    shader.bind()
    shader.uniform_float("color", _COLOR_DIR)
    batch.draw(shader)

    # ---- 3) 触发“面”：法向中间位置、很薄的平面（用户定义的触发区） ----
    # 平面法向 = direction；位置 = sensor 沿该方向的中点；
    # 横向范围 = sensor 在该方向两垂直轴上的完整尺寸（不超出 sensor）。
    mid = (mn[d] + mx[d]) * 0.5
    other = [i for i in range(3) if i != d]
    quad = []
    for a in (mn[other[0]], mx[other[0]]):
        for b in (mn[other[1]], mx[other[1]]):
            p = [0.0, 0.0, 0.0]
            p[d] = mid
            p[other[0]] = a
            p[other[1]] = b
            quad.append(tuple(p))
    world_q = [mw @ Vector(c) for c in quad]
    qpos = [tuple(v[:3]) for v in world_q]

    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    # 正反两个方向都画（两个三角形 + 反向副本），避免背面剔除后看不见。
    tri_pos = [
        qpos[0], qpos[1], qpos[2], qpos[0], qpos[2], qpos[3],
        qpos[3], qpos[2], qpos[1], qpos[3], qpos[1], qpos[0],
    ]
    tri_batch = batch_for_shader(shader, "TRIS", {"pos": tri_pos})
    shader.bind()
    shader.uniform_float("color", _COLOR_FILL)
    tri_batch.draw(shader)
    gpu.state.blend_set("NONE")
    gpu.state.depth_test_set("LESS_EQUAL")

    # ---- 4) 实际检测薄片线框：沿方向厚度 = sensor_thickness ----------------
    corners = _detection_box_corners(mn, mx, d, thickness)
    world = [mw @ Vector(c) for c in corners]
    pos = [tuple(v[:3]) for v in world]  # batch 需要普通 float 序列

    line_pos = [pos[i] for e in _BOX_EDGES for i in e]
    line_batch = batch_for_shader(shader, "LINES", {"pos": line_pos})
    gpu.state.line_width_set(1.5)
    shader.bind()
    shader.uniform_float("color", _COLOR_BOX)
    line_batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _draw() -> None:
    """SpaceView3D draw handler 回调：遍历 scene 里所有传感器并绘制。"""
    if not _scene_bool(SCENE_SHOW_KEY, True):
        return
    scene = bpy.context.scene
    if scene is None:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception as exc:  # GPU 初始化失败等：静默跳过，不影响插件主体
        _ERRORS.append(f"gpu import: {exc}")
        return
    try:
        for obj in scene.objects:
            try:
                if _is_sensor_obj(obj):
                    _draw_sensor(obj, gpu, batch_for_shader)
            except Exception:
                # 单个传感器出错不拖垮整帧绘制；记录便于排查。
                _ERRORS.append(
                    f"sensor {getattr(obj, 'name', '?')}:\n"
                    + traceback.format_exc()
                )
                if len(_ERRORS) > 20:
                    _ERRORS.pop(0)
    except Exception:
        _ERRORS.append(traceback.format_exc())
        if len(_ERRORS) > 20:
            _ERRORS.pop(0)


# ---------------------------------------------------------------- 生命周期


def _refresh_once() -> float | None:
    """低频刷新 timer：勾选 live 时每 ~0.25 s 重绘一次 3D 视图。"""
    try:
        scene = bpy.context.scene
        if scene is None:
            return None
        if not _scene_bool(SCENE_SHOW_KEY, True):
            return None
        if not _scene_bool(SCENE_LIVE_KEY, True):
            return None
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        return None
    return 0.25


def register() -> None:
    """注册 scene 开关属性 + 视口 draw handler + live 刷新 timer。"""
    global _HANDLE, _TIMER
    if not hasattr(bpy.types.Scene, SCENE_SHOW_KEY):
        setattr(
            bpy.types.Scene, SCENE_SHOW_KEY,
            bpy.props.BoolProperty(name="Sensor Overlay", default=True),
        )
    if not hasattr(bpy.types.Scene, SCENE_LIVE_KEY):
        setattr(
            bpy.types.Scene, SCENE_LIVE_KEY,
            bpy.props.BoolProperty(name="Sensor Overlay Live", default=True),
        )
    if _HANDLE is None:
        # Blender 5.x 签名：draw_handler_add(callback, args, region_type, draw_type)
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), "WINDOW", "POST_VIEW"
        )
    if _TIMER is None and not bpy.app.timers.is_registered(_refresh_once):
        _TIMER = bpy.app.timers.register(
            _refresh_once, first_interval=0.25
        )


def unregister() -> None:
    """移除 draw handler / timer / scene 属性（幂等）。"""
    global _HANDLE, _TIMER
    if _HANDLE is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        except Exception:
            pass
        _HANDLE = None
    if _TIMER is not None:
        try:
            bpy.app.timers.unregister(_refresh_once)
        except Exception:
            pass
        _TIMER = None
    for key in (SCENE_SHOW_KEY, SCENE_LIVE_KEY):
        if hasattr(bpy.types.Scene, key):
            try:
                delattr(bpy.types.Scene, key)
            except Exception:
                pass


__all__ = [
    "SCENE_SHOW_KEY",
    "SCENE_LIVE_KEY",
    "register",
    "unregister",
]