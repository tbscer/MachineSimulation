# -*- coding: utf-8 -*-
"""VacuumNozzle 的 RNA 属性注册。

注册 / 反注册都集中在这里,addon.register() 与 addon.unregister() 调用。

属性一览 (都挂到 bpy.types.Object):
- vacuum_nozzle_normal_axis: Enum X/Y/Z,默认 Z
- vacuum_nozzle_working_face_center: FloatVector(3),默认 (0, 0, 0)
- vacuum_nozzle_cube_size: FloatVector(3),默认 (1, 1, 3)

注意 ``sensor_type = "vacuum_nozzle"`` 是 custom property,不挂 RNA。
由 :class:`VacuumNozzleSensor` 在构造时写入(便于 overlay 识别)。
"""

from __future__ import annotations

import bpy


# 属性名常量(便于 overlay / 测试 / 序列化复用)
_PROP_NORMAL_AXIS = "vacuum_nozzle_normal_axis"
_PROP_FACE_CENTER = "vacuum_nozzle_working_face_center"
_PROP_CUBE_SIZE = "vacuum_nozzle_cube_size"


def register() -> None:
    """注册 vacuum nozzle 所需的 Object RNA 属性(幂等)。"""
    if not hasattr(bpy.types.Object, _PROP_NORMAL_AXIS):
        bpy.types.Object.vacuum_nozzle_normal_axis = bpy.props.EnumProperty(
            name="Vacuum Nozzle Normal Axis",
            description=(
                "虚拟立方体延伸方向(吸嘴局部坐标系)。"
                "cube_size 沿该方向的长度即沿法向的拣选区长度。"
            ),
            items=[
                ("X", "X (local)", "Nozzle local X axis"),
                ("Y", "Y (local)", "Nozzle local Y axis"),
                ("Z", "Z (local)", "Nozzle local Z axis"),
            ],
            default="Z",
        )
    if not hasattr(bpy.types.Object, _PROP_FACE_CENTER):
        bpy.types.Object.vacuum_nozzle_working_face_center = (
            bpy.props.FloatVectorProperty(
                name="Vacuum Nozzle Working Face Center",
                description=(
                    "吸嘴局部坐标系下立方体起点。立方体从该点沿 "
                    "normal_axis 正方向延伸 cube_size[normal_axis] 的距离。"
                ),
                size=3,
                default=(0.0, 0.0, 0.0),
                subtype="XYZ",
                precision=4,
            )
        )
    if not hasattr(bpy.types.Object, _PROP_CUBE_SIZE):
        bpy.types.Object.vacuum_nozzle_cube_size = (
            bpy.props.FloatVectorProperty(
                name="Vacuum Nozzle Cube Size",
                description=(
                    "虚拟立方体尺寸(吸嘴局部坐标系)。"
                    "cube_size[normal_axis] 是沿法向的长度(mm),"
                    "另两维是横截面尺寸(mm)。"
                    "默认 (1, 1, 3): 横截面 1×1, 沿法向 3。"
                ),
                size=3,
                default=(1.0, 1.0, 3.0),
                subtype="XYZ",
                min=0.0,
                precision=4,
            )
        )


def unregister() -> None:
    """反注册(幂等)。"""
    for prop in (
        _PROP_NORMAL_AXIS,
        _PROP_FACE_CENTER,
        _PROP_CUBE_SIZE,
    ):
        if hasattr(bpy.types.Object, prop):
            try:
                delattr(bpy.types.Object, prop)
            except Exception:
                pass


__all__ = ["register", "unregister"]