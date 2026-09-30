# -*- coding: utf-8 -*-
"""ApproachSensor 的 RNA 属性注册。

注册 / 反注册都集中在这里, addon.register() 与 addon.unregister() 调用。

属性一览 (都挂到 bpy.types.Object):
- approach_sensor_working_face_center: FloatVector(3), 默认 (0, 0, 0)
  —— 感应盒中心在 sensor 局部坐标系下的偏移
- approach_sensor_cube_size: FloatVector(3), 默认 (1, 1, 1)
  —— 感应盒长宽高 (中心对齐, 半边长 = cube_size[i] / 2)
- approach_sensor_trigger: PointerProperty(Object)

注意 ``sensor_type = "approach"`` 是 custom property, 不挂 RNA。
由 :class:`ApproachSensor` 在构造时写入 (便于 overlay 识别)。

历史变更
--------
- 旧版有 ``approach_sensor_normal_axis`` (Enum X/Y/Z) 字段, 让虚拟盒
  从 ``working_face_center`` 沿 +normal_axis 方向延伸。该设计与"偏离
  中心 + 长宽高" 的 box 模型重复, 且容易让 artist 误配导致 cube 陷进
  物体内部。新版去除 normal_axis, 用中心对齐的 box 模型直接描述感应区。
- 若场景里残留 ``approach_sensor_normal_axis`` 旧 custom property, 反注册
  时会一并清掉, 不影响新 box 行为。
"""

from __future__ import annotations

import bpy


# 属性名常量 (便于 overlay / 测试 / 序列化复用)
_PROP_NORMAL_AXIS_LEGACY = "approach_sensor_normal_axis"  # 旧版字段, 仅在 unregister 时清理
_PROP_FACE_CENTER = "approach_sensor_working_face_center"
_PROP_CUBE_SIZE = "approach_sensor_cube_size"
_PROP_TRIGGER = "approach_sensor_trigger"


def register() -> None:
    """注册 approach sensor 所需的 Object RNA 属性 (幂等)。"""
    # 旧版 ``approach_sensor_normal_axis`` 不再注册 (去除)。
    # 已经在 addon 早版本注册过的 bpy.types.Object.approach_sensor_normal_axis
    # 会在 unregister() 里清掉 (下方)。
    if not hasattr(bpy.types.Object, _PROP_FACE_CENTER):
        bpy.types.Object.approach_sensor_working_face_center = (
            bpy.props.FloatVectorProperty(
                name="Working Face Center (Box Center Offset)",
                description=(
                    "感应盒中心在 sensor 局部坐标系下的偏移。"
                    "立方体以该点为中心, 三维尺寸由 cube_size 决定。"
                    "默认 (0, 0, 0) = 立方体与 sensor mesh 同心。"
                ),
                size=3,
                default=(0.0, 0.0, 0.0),
                subtype="XYZ",
                precision=4,
            )
        )
    if not hasattr(bpy.types.Object, _PROP_CUBE_SIZE):
        bpy.types.Object.approach_sensor_cube_size = (
            bpy.props.FloatVectorProperty(
                name="Approach Sensor Box Size",
                description=(
                    "感应盒尺寸 (sensor 局部坐标系, 中心对齐): "
                    "cube_size = (length, width, height), "
                    "三方向半边长分别为 cube_size[i] / 2。"
                    "默认 (1, 1, 1) = 各方向 ±0.5 的 1×1×1 立方体。"
                ),
                size=3,
                default=(1.0, 1.0, 1.0),
                subtype="XYZ",
                min=0.0,
                precision=4,
            )
        )
    if not hasattr(bpy.types.Object, _PROP_TRIGGER):
        bpy.types.Object.approach_sensor_trigger = (
            bpy.props.PointerProperty(
                name="Approach Sensor Trigger",
                description=(
                    "被 ApproachSensor 检测的对象 (一般是 cylinder 的 "
                    "TouchShime)。其世界 AABB 进入虚拟盒即触发。"
                ),
                type=bpy.types.Object,
            )
        )


def unregister() -> None:
    """反注册 (幂等)。"""
    # 新版 RNA 属性
    for prop in (
        _PROP_FACE_CENTER,
        _PROP_CUBE_SIZE,
        _PROP_TRIGGER,
    ):
        if hasattr(bpy.types.Object, prop):
            try:
                delattr(bpy.types.Object, prop)
            except Exception:
                pass
    # 清理旧版残留: approach_sensor_normal_axis (EnumProperty 注册的)
    if hasattr(bpy.types.Object, _PROP_NORMAL_AXIS_LEGACY):
        try:
            delattr(bpy.types.Object, _PROP_NORMAL_AXIS_LEGACY)
        except Exception:
            pass


__all__ = ["register", "unregister"]