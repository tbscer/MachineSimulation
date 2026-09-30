# -*- coding: utf-8 -*-
"""CylinderProperty:综合对象 ``Cylinder*`` host 的 PropertyGroup。

挂在每个 ``bpy.types.Object.cylinder`` 上,容纳配置:

字段
----
``enabled``              : 总开关,``False`` 时 discovery 跳过。
``work_bar``             : PointerProperty(Object)。运动的实体(在场景里
                           一般是真圆柱体 mesh),cylinder 沿 world X 轴
                           方向驱动它(由两 approach sensor 世界位置
                           推导)。
``touch_shim``           : PointerProperty(Object)。触发片,跟随 work_bar
                           一起运动(由 cylinder runtime 每 tick 同步偏移,
                           不依赖 parent-child 关系)。
``approach_sensor_1``    : PointerProperty(Object)。工作位置 1 的
                           approach sensor。
``approach_sensor_2``    : PointerProperty(Object)。工作位置 2 的
                           approach sensor。
``target_speed``         : FloatProperty。运动速度(沿 axis_dir 单位向量),
                           单位 mm/s,默认 50.0。
``output_1`` / ``output_2`` : BoolProperty。外部控制信号;
                              ``(T, F)`` ⇒ 去工作位置 1,
                              ``(F, T)`` ⇒ 去工作位置 2,
                              ``(T, T)`` 或 ``(F, F)`` ⇒ 不动。
``trigger_obj``          : PointerProperty(Object)。approach sensor 检
                           测的对象(默认指向 touch_shim;UI 上一般不动,
                           由 cylinder runtime 在 discovery 时自动赋值)。

约定
----
- 不引入 ``rail`` 部件 —— 运动方向由 ``approach_sensor_2.world_pos`` 与
  ``approach_sensor_1.world_pos`` 的差推出。
- 没有 ``target_axis_index`` 字段 —— 推导是自动的。
- 没有 ``home_position`` 字段 —— 初始位置来自 work_bar 在构造时刻的
  world 位置,后续 ``current_x`` 是相对该初始位置在 axis_dir 上的位移。
"""

import bpy


class CylinderProperty(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(default=True)

    # ---- host refs ----
    work_bar: bpy.props.PointerProperty(
        name="Work Bar",
        description=(
            "运动的实体(场景里是真圆柱 mesh)。cylinder 沿 axis_dir "
            "(由两 approach sensor 位置推导)驱动 work_bar 的 world 位置。"
        ),
        type=bpy.types.Object,
    )
    touch_shim: bpy.props.PointerProperty(
        name="Touch Shim",
        description=(
            "触发片,跟随 work_bar 一起运动。"
            "由 cylinder runtime 每 tick 同步 world 位置(不依赖父子关系)。"
            "approach sensor 检测它。"
        ),
        type=bpy.types.Object,
    )
    approach_sensor_1: bpy.props.PointerProperty(
        name="Approach Sensor 1",
        description="工作位置 1 的 approach sensor(虚拟立方体传感器)",
        type=bpy.types.Object,
    )
    approach_sensor_2: bpy.props.PointerProperty(
        name="Approach Sensor 2",
        description="工作位置 2 的 approach sensor(虚拟立方体传感器)",
        type=bpy.types.Object,
    )

    # ---- motion ----
    target_speed: bpy.props.FloatProperty(
        name="Target Speed",
        description="work_bar 沿 axis_dir 方向运动的速度(mm/s)",
        default=50.0,
        min=0.0,
        precision=3,
        subtype="DISTANCE",
    )

    # ---- 外部控制信号 ----
    output_1: bpy.props.BoolProperty(
        name="Output 1",
        description=(
            "外部控制信号位 1。与 output_2 组合决定目标工作位置: "
            "(T, F) -> 工作位置 1, (F, T) -> 工作位置 2, "
            "(T, T) / (F, F) -> 不动。"
        ),
        default=False,
    )
    output_2: bpy.props.BoolProperty(
        name="Output 2",
        description="外部控制信号位 2(语义见 output_1)",
        default=False,
    )

    # ---- approach sensor 检测对象(自动从 touch_shim 同步) ----
    trigger_obj: bpy.props.PointerProperty(
        name="Trigger Object",
        description=(
            "approach sensor 检测的对象。默认 cylinder runtime 在 "
            "discovery 时自动指向 touch_shim。"
        ),
        type=bpy.types.Object,
    )


def register() -> None:
    """注册 PropertyGroup + 挂到 ``bpy.types.Object.cylinder``。"""
    bpy.utils.register_class(CylinderProperty)
    bpy.types.Object.cylinder = bpy.props.PointerProperty(
        type=CylinderProperty
    )


def unregister() -> None:
    """反注册(幂等)。"""
    if hasattr(bpy.types.Object, "cylinder"):
        try:
            delattr(bpy.types.Object, "cylinder")
        except Exception:
            pass
    try:
        bpy.utils.unregister_class(CylinderProperty)
    except Exception:
        pass


__all__ = ["CylinderProperty", "register", "unregister"]