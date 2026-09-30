"""ApproachSensorProperty:挂在 ``bpy.types.Object.approach_sensor`` 上的配置容器。

字段
----
``enabled``              : BoolProperty, 默认 ``True`` (与 LinearAxis /
                           Cylinder 一致; 一次 discovery 自动注册全部
                           名字以 ``ApproachSensor`` 开头的 host, 无需手动开)。
``trigger_group_name``   : StringProperty (可选, 默认空字符串)。为
                           后续"按名字前缀过滤扫描范围"预留扩展位,
                           本轮**不参与** runtime 行为 (任何非空
                           mesh 都计为触发源), 保留 cfg 入口便于
                           后续在不破环 runtime 的前提下加语义。

不引入什么
----------
- 不引入 ``trigger_obj`` 指针 —— 旧版 ApproachSensor 需要它,
  本 ApproachSensor module 不需要特定 trigger 对象, **每 tick 自动扫描
  场景全部 mesh**, 与原 ApproachSensor 组件的语义不同。
- 不引入 ``sensor_type`` 字段 —— 直接读 host 上的 custom property
  ``sensor_type`` (由 :class:`components.approach_sensor.ApproachSensor`
  在构造时写)。这是 sensor mesh 的内禀属性, 不该由 ApproachSensorProperty
  重复声明。

重命名历史
----------
旧版 ``SensorProperty`` / ``bpy.types.Object.sensor`` 已改名为
``ApproachSensorProperty`` / ``bpy.types.Object.approach_sensor``,
原因: LinearAxis 的 UTypeSensor 已占用 ``sensor_direction`` / ``sensor_thickness``
等命名空间, 共享 ``sensor`` PropertyGroup 名会导致 artist 在面板里看到
两个同名的 PropertyGroup 入口、discovery 又只认其中一个, 排查非常困难。
"""

import bpy


class ApproachSensorProperty(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description=(
            "ApproachSensor module 总开关。默认 True —— 一次性 discovery 会按 enabled "
            "门控注册名字以 ``ApproachSensor`` 开头的 host (与 LinearAxis / Cylinder "
            "同构)。取消勾选后下次 Refresh 才会反注册 (与 axis 类一致语义)。"
        ),
        default=True,
    )

    trigger_group_name: bpy.props.StringProperty(
        name="Trigger Group Name",
        description=(
            "可选: 扫描候选 mesh 时的名字前缀过滤 (留空 = 扫描全部场景 "
            "mesh)。本轮保留为扩展位, runtime 不强行限制扫描范围。"
        ),
        default="",
    )


def register() -> None:
    """注册 PropertyGroup + 挂到 ``bpy.types.Object.approach_sensor``。"""
    bpy.utils.register_class(ApproachSensorProperty)
    bpy.types.Object.approach_sensor = bpy.props.PointerProperty(
        type=ApproachSensorProperty
    )


def unregister() -> None:
    """反注册 (幂等)。"""
    if hasattr(bpy.types.Object, "approach_sensor"):
        try:
            delattr(bpy.types.Object, "approach_sensor")
        except Exception:
            pass
    try:
        bpy.utils.unregister_class(ApproachSensorProperty)
    except Exception:
        pass


__all__ = ["ApproachSensorProperty", "register", "unregister"]