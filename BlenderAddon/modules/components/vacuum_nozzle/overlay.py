# -*- coding: utf-8 -*-
"""VacuumNozzle 视口可视化。

识别方式:对象名以 ``VacuumNozzle`` 开头
(:func:`modules.VacuumNozzle.naming.is_host`)—— **不再**依赖 ``sensor_mesh``
或 ``sensor_type`` custom property。因此即使吸嘴还没打开真空开关(未注册
runtime),overlay 照样画得出来,便于先把感应区调好再 On。
绘制内容(与 :class:`ApproachSensor` 同构):
- 12 条棱线框
- 6 个半透明 quad 面
- normal_axis 方向白线:从 ``working_face_center`` 沿法向延伸 cube_size+5 mm

颜色由 runtime 每 tick 写回 host 的三个 bool 决定:

===============  ==========================  ========
``vacuum_nozzle_on``  ``vacuum_nozzle_holding``  颜色
===============  ==========================  ========
False(Off)       —                           绿(默认态)
True             False                       琥珀(On,感应区内暂无物体)
True             True                        青(已吸住物体)
===============  ==========================  ========

**显示策略:永远显示**
------------------------
本 overlay **不受** ``Scene.ms_show_sensor_overlay`` /
``Scene.ms_sensor_overlay_live`` 两个开关影响(那两个是给
``UTypeSensor`` / ``ApproachSensor`` overlay 用的)。只要场景里有
``VacuumNozzle*`` 对象,感应区盒子就一直画着 —— 它是调感应区的
**工作界面**,被别的开关连坐关掉会让人以为功能坏了。

刷新由 ``_refresh_once`` 的 0.25s timer 驱动,但采用**变化检测**:
仅当“外观输入”真的变了(host 的名字 / on / holding / 可见性 /
感应区几何 / world 矩阵)才 ``area.tag_redraw()``。这样既保证 overlay
始终跟得上变化(包括被轴带着运动、开关真空、改感应区尺寸),又不至于
让视口永久 4Hz 空转烧 GPU。

想临时藏掉某个吸嘴的盒子:把那个对象在 Outliner 里眼晴图标关掉
(``hide_viewport``)—— 那是逐对象的显式意图,overlay 会尊重。
"""

from __future__ import annotations

import traceback

import bpy


# 本 overlay **没有**开关 scene key —— 永远显示。
# (历史上它复用 ``ms_show_sensor_overlay`` / ``ms_sensor_overlay_live``,
#  但那样会被别的 sensor overlay 开关连坐关掉。详见模块 docstring。)

# host 上的运行态标志(runtime 写,overlay 只读)。
# 与 modules/VacuumNozzle/runtime.py 的 FLAG_* 常量保持同名;这里刻意
# 复制字符串而不 import,避免组件层反向依赖模块层。
_FLAG_ON = "vacuum_nozzle_on"
_FLAG_HOLDING = "vacuum_nozzle_holding"

# 颜色
_COLOR_OFF = (0.30, 0.95, 0.30, 1.0)        # 绿:Off(默认态)
_COLOR_OFF_FILL = (0.30, 0.95, 0.30, 0.18)
_COLOR_SENSING = (1.00, 0.72, 0.12, 1.0)    # 琥珀:On,感应区内暂无物体
_COLOR_SENSING_FILL = (1.00, 0.72, 0.12, 0.22)
_COLOR_HOLDING = (0.20, 0.85, 1.00, 1.0)    # 青:已吸住
_COLOR_HOLDING_FILL = (0.20, 0.85, 1.00, 0.30)
_COLOR_AXIS = (1.0, 1.0, 1.0, 1.0)          # 法向白线

# 立方体 12 条棱的角点索引对
_BOX_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 0),    # bottom
    (4, 5), (5, 6), (6, 7), (7, 4),    # top
    (0, 4), (1, 5), (2, 6), (3, 7),    # pillars
)

# 模块状态
_HANDLE = None
_TIMER = None
_ERRORS = []
_LAST_SIG = None


# ---- host 判定 ----

try:
    from ....modules.VacuumNozzle.naming import is_host as _is_host
except ImportError:  # pragma: no cover —— 离线/直接脚本 import 路径
    try:
        from modules.VacuumNozzle.naming import is_host as _is_host  # type: ignore
    except ImportError:
        def _is_host(obj) -> bool:
            """兜底:``VacuumNozzle`` 前缀判定。"""
            name = getattr(obj, "name", "") or ""
            return name.startswith("VacuumNozzle")


# ---- helpers ----


def _is_vacuum_nozzle(obj) -> bool:
    """host 判定:名字前缀 ``VacuumNozzle``,且未被隐藏。

    刻意**不**限制 ``obj.type`` —— host 可以是 EMPTY 也可以是 MESH
    (通常是代表吸嘴的圆柱体 MESH)。只要拿得到 ``matrix_world`` 就能画。
    """
    if obj is None:
        return False
    if getattr(obj, "hide_viewport", False):
        return False
    return bool(_is_host(obj))


def _safe_call_factory():
    """在 GPU 不存在的环境(离线测试 / headless)下 overlay 应静默跳过。
    这里把 import + 失败缓存起来,只记录一次错误,避免每个 host 都重试。
    """
    cache = {"gpu": None, "batch": None, "failed": False}

    def _try():
        if cache["failed"]:
            return None
        if cache["gpu"] is None:
            try:
                import gpu
                from gpu_extras.batch import batch_for_shader
                cache["gpu"] = gpu
                cache["batch"] = batch_for_shader
            except Exception as exc:
                _ERRORS.append(f"gpu import: {exc}")
                cache["failed"] = True
                return None
        return cache["gpu"], cache["batch"]

    return _try


_GPU_TRY = _safe_call_factory()


def _draw_one(obj, gpu, batch_for_shader):
    """画一个 vacuum nozzle 的感应区立方体 + 法向白线。"""
    try:
        from mathutils import Vector  # type: ignore  # noqa: F401
    except Exception:
        return

    on = False
    holding = False
    try:
        on = bool(obj.get(_FLAG_ON, False))
        holding = bool(obj.get(_FLAG_HOLDING, False))
    except Exception:
        pass

    # 感应区锚点就是 host 自己
    try:
        from .vacuum_nozzle import VacuumNozzleSensor
        sensor = VacuumNozzleSensor(obj)
    except Exception:
        return

    cube_corners = sensor.get_cube_world_corners()
    if cube_corners is None:
        return
    axis_endpoints = sensor.get_normal_axis_world_endpoints()
    if axis_endpoints is None:
        return

    if holding:
        color_edge, color_fill = _COLOR_HOLDING, _COLOR_HOLDING_FILL
    elif on:
        color_edge, color_fill = _COLOR_SENSING, _COLOR_SENSING_FILL
    else:
        color_edge, color_fill = _COLOR_OFF, _COLOR_OFF_FILL

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")

    # ---- 1) 线框 ----
    pos_line = []
    for a, b in _BOX_EDGES:
        pos_line.append((cube_corners[a].x, cube_corners[a].y, cube_corners[a].z))
        pos_line.append((cube_corners[b].x, cube_corners[b].y, cube_corners[b].z))
    line_batch = batch_for_shader(shader, "LINES", {"pos": pos_line})
    gpu.state.line_width_set(2.0)
    shader.bind()
    shader.uniform_float("color", color_edge)
    line_batch.draw(shader)
    gpu.state.line_width_set(1.0)

    # ---- 2) 半透明 6 面填充(12 个三角形,正反两面) ----
    # 6 quad 面:每面 4 角点;每面 2 三角形 = 6 顶点;正反 = 12 顶点
    quad_indices = (
        (0, 1, 2, 3),  # -Z
        (4, 5, 6, 7),  # +Z
        (0, 1, 5, 4),  # -Y
        (2, 3, 7, 6),  # +Y
        (0, 3, 7, 4),  # -X
        (1, 2, 6, 5),  # +X
    )
    tri_pos = []
    for q in quad_indices:
        a, b, c, d = q
        # 正向
        tri_pos.append((cube_corners[a].x, cube_corners[a].y, cube_corners[a].z))
        tri_pos.append((cube_corners[b].x, cube_corners[b].y, cube_corners[b].z))
        tri_pos.append((cube_corners[c].x, cube_corners[c].y, cube_corners[c].z))
        tri_pos.append((cube_corners[a].x, cube_corners[a].y, cube_corners[a].z))
        tri_pos.append((cube_corners[c].x, cube_corners[c].y, cube_corners[c].z))
        tri_pos.append((cube_corners[d].x, cube_corners[d].y, cube_corners[d].z))
        # 反向(避免背面剔除)
        tri_pos.append((cube_corners[a].x, cube_corners[a].y, cube_corners[a].z))
        tri_pos.append((cube_corners[d].x, cube_corners[d].y, cube_corners[d].z))
        tri_pos.append((cube_corners[c].x, cube_corners[c].y, cube_corners[c].z))
        tri_pos.append((cube_corners[a].x, cube_corners[a].y, cube_corners[a].z))
        tri_pos.append((cube_corners[c].x, cube_corners[c].y, cube_corners[c].z))
        tri_pos.append((cube_corners[b].x, cube_corners[b].y, cube_corners[b].z))
    tri_batch = batch_for_shader(shader, "TRIS", {"pos": tri_pos})
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    shader.bind()
    shader.uniform_float("color", color_fill)
    tri_batch.draw(shader)
    gpu.state.blend_set("NONE")
    gpu.state.depth_test_set("LESS_EQUAL")

    # ---- 3) 法向白线(从 working_face_center 沿 normal 延伸) ----
    (p0, p1) = axis_endpoints
    line2 = [
        (p0.x, p0.y, p0.z),
        (p1.x, p1.y, p1.z),
    ]
    line2_batch = batch_for_shader(shader, "LINES", {"pos": line2})
    gpu.state.line_width_set(1.5)
    shader.bind()
    shader.uniform_float("color", _COLOR_AXIS)
    line2_batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _draw():
    """SpaceView3D draw handler 回调。

    **不受** ``ms_show_sensor_overlay`` 控制 —— 本 overlay 永远显示。
    逐对象的隐藏靠 ``obj.hide_viewport``(由 :func:`_is_vacuum_nozzle` 尊重)。
    """
    scene = bpy.context.scene
    if scene is None:
        return
    got = _GPU_TRY()
    if got is None:
        return
    gpu, batch_for_shader = got
    try:
        for obj in scene.objects:
            try:
                if _is_vacuum_nozzle(obj):
                    _draw_one(obj, gpu, batch_for_shader)
            except Exception:
                _ERRORS.append(
                    f"vacuum nozzle {getattr(obj, 'name', '?')}:\n"
                    + traceback.format_exc()
                )
                if len(_ERRORS) > 20:
                    _ERRORS.pop(0)
    except Exception:
        _ERRORS.append(traceback.format_exc())
        if len(_ERRORS) > 20:
            _ERRORS.pop(0)


def _overlay_signature():
    """overlay 外观的全部输入摘要 —— 变了才需要重画。

    包含:host 名 / on / holding 标志 / 可见性 / 感应区几何(axis +
    cube_size + face_center)/ world 矩阵。其中 world 矩阵保证“吸嘴被轴
    带着跑”时每帧都能跟上;几何三属性保证“在面板里改感应区尺寸”能
    立即反映。

    拿不到 scene 时返回 ``None``(调用方跳过比较)。
    """
    scene = bpy.context.scene
    if scene is None:
        return None
    sig = []
    for obj in getattr(scene, "objects", ()) or ():
        try:
            if not _is_host(obj):
                continue
            mw = obj.matrix_world
            sig.append((
                getattr(obj, "name", "?"),
                bool(obj.get(_FLAG_ON, False)),
                bool(obj.get(_FLAG_HOLDING, False)),
                bool(getattr(obj, "hide_viewport", False)),
                getattr(obj, "vacuum_nozzle_normal_axis", None),
                tuple(obj.vacuum_nozzle_cube_size),
                tuple(obj.vacuum_nozzle_working_face_center),
                tuple(
                    round(v, 5)
                    for row in mw for v in row
                ),
            ))
        except Exception:
            # stale 引用 / 缺属性:忽略这个对象,不影响其它吸嘴
            continue
    return tuple(sig)


def _refresh_once():
    """0.25s timer:只在 overlay 外观真的变了时才 tag_redraw。

    不用 ``ms_sensor_overlay_live`` 闸口(本 overlay 永远显示),但也不无脑
    每帧 tag —— 静止时视口可以真正闲下来。
    """
    global _LAST_SIG
    try:
        if bpy.context.scene is None:
            return 0.25
        try:
            screen = bpy.context.screen
            areas = list(screen.areas) if screen is not None else []
        except Exception:
            areas = []
        if not areas:
            return 0.25
        sig = _overlay_signature()
        if sig != _LAST_SIG:
            _LAST_SIG = sig
            for area in areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        _ERRORS.append(traceback.format_exc())
        if len(_ERRORS) > 20:
            _ERRORS.pop(0)
    return 0.25


def register() -> None:
    """注册 draw handler + 变化检测刷新 timer(幂等)。"""
    global _HANDLE, _TIMER, _LAST_SIG
    if _HANDLE is None:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), "WINDOW", "POST_VIEW"
        )
    if _TIMER is None and not bpy.app.timers.is_registered(_refresh_once):
        _LAST_SIG = None          # 注册时先强制一次 redraw
        _TIMER = bpy.app.timers.register(_refresh_once, first_interval=0.25)


def unregister() -> None:
    """移除 draw handler / timer(幂等)。"""
    global _HANDLE, _TIMER, _LAST_SIG
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
    _LAST_SIG = None


__all__ = ["register", "unregister"]