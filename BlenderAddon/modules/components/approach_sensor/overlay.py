# -*- coding: utf-8 -*-
"""ApproachSensor 视口可视化。

读取 ``sensor_type == "approach"`` 的 mesh,绘制其虚拟感应盒:
- 8 条棱线框:未触发 = 琥珀,触发 = 红
- 6 个半透明 quad 同色 alpha 0.18

盒子几何:中心 = working_face_center (sensor 局部系),尺寸 = cube_size
(中心对齐的轴对齐盒,三维独立)。

开关:与现有 sensor overlay 共用 scene 键
``bpy.types.Scene.ms_show_sensor_overlay``(默认 True),
不引入新开关以保持一致。
"""

from __future__ import annotations

import traceback

import bpy


# 复用 sensor overlay 的开关 scene key(避免再加一个开关)
_SHOW_KEY = "ms_show_sensor_overlay"
_LIVE_KEY = "ms_sensor_overlay_live"

# 颜色
_COLOR_OFF = (1.0, 0.62, 0.10, 1.0)      # 琥珀 (未触发)
_COLOR_OFF_FILL = (1.0, 0.55, 0.12, 0.18)
_COLOR_ON = (1.0, 0.18, 0.18, 1.0)        # 红 (触发)
_COLOR_ON_FILL = (1.0, 0.10, 0.10, 0.30)

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


# ---- helpers ----


def _scene_bool(key: str, default: bool) -> bool:
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


def _is_approach_sensor(obj) -> bool:
    if obj is None or obj.type != "MESH":
        return False
    if getattr(obj, "hide_viewport", False):
        return False
    try:
        return obj.get("sensor_type") == "approach"
    except Exception:
        return False


def _safe_call_factory():
    """在 GPU 不存在的环境(离线测试 / headless)下 overlay 应静默跳过。
    这里把 import + 失败缓存起来,只记录一次错误,避免每个 sensor 都重试。
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
    """画一个 approach sensor 的虚拟立方体 + 法向白线。"""
    try:
        from mathutils import Vector  # type: ignore  # Blender 环境
    except Exception:
        return

    triggered = False
    try:
        triggered = bool(obj.get("is_triggered", False))
    except Exception:
        pass

    # 在 sensor obj 上做最简几何:用 ApproachSensor class 拿 8 个世界角点
    try:
        from .approach_sensor import ApproachSensor
        sensor = ApproachSensor(obj, trigger_obj=None)  # 只读几何
    except Exception:
        return

    cube_corners = sensor.get_cube_world_corners()
    if cube_corners is None:
        return

    color_edge = _COLOR_ON if triggered else _COLOR_OFF
    color_fill = _COLOR_ON_FILL if triggered else _COLOR_OFF_FILL

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


def _draw():
    """SpaceView3D draw handler 回调。"""
    if not _scene_bool(_SHOW_KEY, True):
        return
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
                if _is_approach_sensor(obj):
                    _draw_one(obj, gpu, batch_for_shader)
            except Exception:
                _ERRORS.append(
                    f"approach sensor {getattr(obj, 'name', '?')}:\n"
                    + traceback.format_exc()
                )
                if len(_ERRORS) > 20:
                    _ERRORS.pop(0)
    except Exception:
        _ERRORS.append(traceback.format_exc())
        if len(_ERRORS) > 20:
            _ERRORS.pop(0)


def _refresh_once():
    try:
        scene = bpy.context.scene
        if scene is None:
            return None
        if not _scene_bool(_SHOW_KEY, True):
            return None
        if not _scene_bool(_LIVE_KEY, True):
            return None
        for area in bpy.context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        return None
    return 0.25


def register() -> None:
    """注册 draw handler + 低频刷新 timer(幂等)。"""
    global _HANDLE, _TIMER
    if _HANDLE is None:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), "WINDOW", "POST_VIEW"
        )
    if _TIMER is None and not bpy.app.timers.is_registered(_refresh_once):
        _TIMER = bpy.app.timers.register(_refresh_once, first_interval=0.25)


def unregister() -> None:
    """移除 draw handler / timer(幂等)。"""
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


__all__ = ["register", "unregister"]