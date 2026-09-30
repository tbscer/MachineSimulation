# -*- coding: utf-8 -*-
"""VacuumNozzle 组件 —— 沿用法向的虚拟立方体拣选检测器。

独立于 :class:`ApproachSensor`,但**复用**其几何数学(直接 import
``_compute_cube_bounds`` / ``_oriented_box_overlaps_aabb`` 等 privates,
下方代码有明确注释)。VacuumNozzleSensor 不维护 ``is_triggered`` 翻转
状态(那只是 ApproachSensor 检测器的一个语义);对外只暴露无状态的
``is_in_area(target_obj)`` 查询接口。

感应区的锚点就是**真空吸嘴自己**(``VacuumNozzle*`` host),不需要单独的
``sensor_mesh``。

配置(RNA on host,任意 Object 上都可用,本组件只从 host 读):
- :attr:`bpy.types.Object.vacuum_nozzle_normal_axis` —— Enum X/Y/Z,默认 Z
- :attr:`bpy.types.Object.vacuum_nozzle_cube_size` —— FloatVector(3),
  其中 ``cube_size[normal_axis]`` 是沿法向长度,另两维是横截面尺寸。
  默认 ``(1, 1, 3)`` 即横截面 1×1、沿 Z 长 3。
- :attr:`bpy.types.Object.vacuum_nozzle_working_face_center` ——
  FloatVector(3),host 局部坐标系下的立方体起点。

子模块组织:
- ``vacuum_nozzle.py`` —— 主类(可在离线环境 import,不依赖 bpy)
- ``rna.py`` —— 注册 ``bpy.types.Object.vacuum_nozzle_*``(需要 Blender)
- ``overlay.py`` —— 3D 视口可视化(需要 Blender)

为避免离线 import 链路在 rna/overlay 上炸(它们顶层 ``import bpy``),
``register_rna`` / ``unregister_rna`` / ``register_overlay`` /
``unregister_overlay`` 均采用 lazy import:仅在调用时才真正加载对应子模块。
"""

from __future__ import annotations

from .vacuum_nozzle import VacuumNozzleSensor


def register_rna() -> None:
    """注册 vacuum nozzle 所需的 Object RNA 属性(lazy import)。"""
    from . import rna as _rna  # noqa: WPS433 —— 必须 lazy,顶层 import 会触发 bpy
    _rna.register()


def unregister_rna() -> None:
    """反注册 RNA 属性(lazy import)。"""
    from . import rna as _rna  # noqa: WPS433
    _rna.unregister()


def register_overlay() -> None:
    """注册 3D 视口 overlay draw handler(lazy import)。"""
    from . import overlay as _ov  # noqa: WPS433
    _ov.register()


def unregister_overlay() -> None:
    """反注册 overlay(lazy import)。"""
    from . import overlay as _ov  # noqa: WPS433
    _ov.unregister()


__all__ = [
    "VacuumNozzleSensor",
    "register_rna",
    "unregister_rna",
    "register_overlay",
    "unregister_overlay",
]