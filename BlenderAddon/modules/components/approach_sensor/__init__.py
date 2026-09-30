# -*- coding: utf-8 -*-
"""ApproachSensor 组件 —— 中心对齐的轴对齐盒触发检测。

独立于 :class:`BaseSensor` / :class:`UTypeSensor`(几何模型不同:
UTypeSensor 用 sensor mesh 自身局部 AABB 切薄片;ApproachSensor
用一个独立配置的虚拟盒,可能比 sensor mesh 大也可能比 sensor mesh 小)。

配置 (RNA on sensor mesh):
- :attr:`bpy.types.Object.approach_sensor_cube_size` —— FloatVector(3),
  感应盒尺寸 (长宽高), 默认 ``(1, 1, 1)``。
- :attr:`bpy.types.Object.approach_sensor_working_face_center` ——
  FloatVector(3), sensor 局部坐标系下的盒**中心偏移** (不是起点)。
  默认 ``(0, 0, 0)`` 表示盒与 sensor mesh 同心。
- :attr:`bpy.types.Object.approach_sensor_trigger` —— PointerProperty
  (Object), 检测对象(一般就是 cylinder 的 TouchShime)。

历史变更
--------
旧版 ``approach_sensor_normal_axis`` (Enum X/Y/Z) + cube_size[d] 描述
"沿 normal_axis 方向凸出的探针"。新版去除 normal_axis, 盒中心对齐
(sensor 局部) 描述感应区, 三个维度独立。详见 :mod:`.approach_sensor`。

子模块组织:
- ``approach_sensor.py`` —— 主类(可在离线环境 import,不依赖 bpy)
- ``rna.py`` —— 注册 ``bpy.types.Object.approach_sensor_*``(需要 Blender)
- ``overlay.py`` —— 3D 视口可视化(需要 Blender)

为避免离线 import 链路在 rna/overlay 上炸(它们顶层 ``import bpy``),
``register_rna`` / ``unregister_rna`` / ``register_overlay`` / ``unregister_overlay``
均采用 lazy import:仅在调用时才真正加载对应子模块。
"""

from __future__ import annotations

from .approach_sensor import ApproachSensor


def register_rna() -> None:
    """注册 approach sensor 所需的 Object RNA 属性(lazy import)。"""
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
    "ApproachSensor",
    "register_rna",
    "unregister_rna",
    "register_overlay",
    "unregister_overlay",
]