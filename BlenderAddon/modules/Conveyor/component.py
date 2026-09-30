# -*- coding: utf-8 -*-
"""ConveyorProperty:综合对象 ``Conveyor*`` host 的 PropertyGroup。

挂在每个 ``bpy.types.Object.conveyor`` 上,容纳配置:

字段
----
``enabled``              : 总开关,``False`` 时 discovery 跳过(默认 True)。
``drive_roller``         : PointerProperty(Object)。主动旋转的滚轮 mesh,
                           用来推算 belt 的运动方向(drive → idler)。
``idler_roller``         : PointerProperty(Object)。从动滚轮 mesh,
                           支撑皮带;runtime 构造期校验两者世界位置不重合。
``belt``                 : PointerProperty(Object)。皮带 mesh(静态),
                           也是 belt_uv_offset 自定义属性的写入目标,
                           同时是材质 Mapping 节点 driver 的驱动源。
``target_speed``         : FloatProperty。皮带线速度,单位 mm/s,默认 50.0。
                           内部按 BU/s 换算(``0.001`` 系数,与 LinearAxis 一致)。
``direction``            : EnumProperty。``"forward"``(主动 → 从动,默认)/
                           ``"reverse"``(从动 → 主动)。runtime 把它转成
                           :data:`DIR_FORWARD` / :data:`DIR_REVERSE` 两个
                           ``+1`` / ``-1`` 数值。
``friction``             : FloatProperty。0.0..1.0 之间,默认 1.0。
                           1.0 = 完全抓地(物体瞬间匹配皮带速度);
                           0.0 = 完全打滑(物体不动);
                           介于二者之间时,物体当前速度沿 belt_dir 与皮带
                           速度做一维 lerp。
``running``              : BoolProperty。默认 False —— conveyor 一开始是
                           静止状态,artist 在 UI 上点 Start 后才进入
                           ``STATE_RUNNING``。

约定
----
- 主动 / 从动轮 + belt 共 3 个必填 PointerProperty,缺一则在 console 打印
  pointer 缺失提示并跳过(discovery 阶段)。
- 两个 roller 必须不同 Object(否则推导 belt_dir 无定义,同 Cylinder 的
  approach_sensor 校验)。
- ``friction`` / ``direction`` 改变不需要刷新 discovery,运行时每 tick
  镜像 cfg 到 instance(同 Cylinder output_1/2)。
- 不引入 motion 字段(``axis_cmd_*`` / ``home_*``)—— conveyor 不是 axis,
  不需要 home 修复方案。
"""

import bpy


# ---- direction 枚举常量 ----

DIR_FORWARD = 1
DIR_REVERSE = -1
DIR_CHOICES = (
    ("forward", "Forward", "由主动轮推向从动轮(drive → idler)"),
    ("reverse", "Reverse", "由从动轮推向主动轮(idler → drive)"),
)


class ConveyorProperty(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description=(
            "Conveyor 总开关。False 时 discovery 跳过(模块不会被注册)"
        ),
        default=True,
    )

    # ---- host refs(三个必填 PointerProperty)----
    drive_roller: bpy.props.PointerProperty(
        name="Drive Roller",
        description=(
            "主动滚轮 mesh。Conveyor 由它决定传送方向:"
            "默认(direction=forward)时物体从 drive 端推向 idler 端。"
        ),
        type=bpy.types.Object,
    )
    idler_roller: bpy.props.PointerProperty(
        name="Idler Roller",
        description=(
            "从动滚轮 mesh。不主动旋转,仅作支撑。"
            "两 roller 世界位置不重合才能推导 belt_dir。"
        ),
        type=bpy.types.Object,
    )
    belt: bpy.props.PointerProperty(
        name="Belt",
        description=(
            "皮带 mesh(静态)。runtime 往它身上写 belt_uv_offset 自定义"
            "属性供材质 Mapping 节点 driver 用。也可放其它装饰物。"
        ),
        type=bpy.types.Object,
    )

    # ---- motion ----
    target_speed: bpy.props.FloatProperty(
        name="Target Speed",
        description=(
            "皮带线速度。面板单位 mm/s,内部按 BU/s 工作 "
            "(1 BU = 1 mm 时换算系数 0.001,与 LinearAxis 一致)。"
        ),
        default=50.0,
        min=0.0,
        precision=3,
        subtype="DISTANCE",
    )
    direction: bpy.props.EnumProperty(
        name="Direction",
        description=(
            "传送方向。forward = drive → idler;reverse = idler → drive。"
        ),
        items=DIR_CHOICES,
        default="forward",
    )
    friction: bpy.props.FloatProperty(
        name="Friction",
        description=(
            "0.0..1.0。1.0 = 皮带完全抓地(物体瞬间匹配皮带速度);"
            "0.0 = 完全打滑(物体不被推动);"
            "介于二者之间时,物体当前速度沿 belt_dir 与皮带速度做一维 lerp。"
        ),
        default=1.0,
        min=0.0,
        max=1.0,
        precision=3,
    )

    # ---- control ----
    running: bpy.props.BoolProperty(
        name="Running",
        description=(
            "Conveyor 启停开关。True = 启动(STATE_RUNNING);"
            "False = 停止(STATE_IDLE,不扫场景、不写 linear_velocity)。"
        ),
        default=False,
    )


def register() -> None:
    """注册 PropertyGroup + 挂到 ``bpy.types.Object.conveyor``。"""
    bpy.utils.register_class(ConveyorProperty)
    bpy.types.Object.conveyor = bpy.props.PointerProperty(
        type=ConveyorProperty
    )


def unregister() -> None:
    """反注册(幂等)。"""
    if hasattr(bpy.types.Object, "conveyor"):
        try:
            delattr(bpy.types.Object, "conveyor")
        except Exception:
            pass
    try:
        bpy.utils.unregister_class(ConveyorProperty)
    except Exception:
        pass


__all__ = [
    "ConveyorProperty",
    "DIR_FORWARD",
    "DIR_REVERSE",
    "DIR_CHOICES",
    "register",
    "unregister",
]