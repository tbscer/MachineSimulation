# -*- coding: utf-8 -*-
"""VacuumNozzleProperty:真空吸嘴 host(``VacuumNozzle*``)的 PropertyGroup。

挂在每个 ``bpy.types.Object.vacuum_nozzle`` 上,容纳配置:

字段
----
``enabled``     : BoolProperty,默认 **False**(Off)。真空开关 —— On 时把
                  感应区内的物体吸成自己的子对象,Off 时把子对象全部拆解
                  回场景目录。

注意
----
- **没有 ``sensor_mesh``** —— 感应区直接锚定在 host 自己身上(吸嘴通常画
  成一个圆柱体,它自己就是锚点)。感应区几何由 host 上的
  ``vacuum_nozzle_normal_axis`` / ``vacuum_nozzle_cube_size`` /
  ``vacuum_nozzle_working_face_center`` 三个 RNA 属性描述。
- **没有 ``trigger_obj``** —— 旧版那个“只吸指定一个对象”的可选白名单已被
  **删除**。它是个静默陷阱:旧 ``.blend`` 里普遍残留着这个字段,于是自动
  扫描被静默绕过,表现为“吸不到任何东西”。现在只有一条路径:自动扫描
  感应区内的可吸物体。
- 不引入 motion 字段(速度 / 距离):本模块不产生运动,只做父子切换。
"""

from __future__ import annotations

import bpy


class VacuumNozzleProperty(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Vacuum On",
        description=(
            "真空开关(默认 Off)。On:把感应区内的物体吸成子对象,"
            "吸嘴被别的对象带着运动时它们跟着走;"
            "Off:把持有的子对象全部拆解,放回场景目录。"
        ),
        default=False,
    )


def register() -> None:
    """注册 PropertyGroup + 挂到 ``bpy.types.Object.vacuum_nozzle``。"""
    bpy.utils.register_class(VacuumNozzleProperty)
    bpy.types.Object.vacuum_nozzle = bpy.props.PointerProperty(
        type=VacuumNozzleProperty
    )


def unregister() -> None:
    """反注册(幂等)。"""
    if hasattr(bpy.types.Object, "vacuum_nozzle"):
        try:
            delattr(bpy.types.Object, "vacuum_nozzle")
        except Exception:
            pass
    try:
        bpy.utils.unregister_class(VacuumNozzleProperty)
    except Exception:
        pass


__all__ = ["VacuumNozzleProperty", "register", "unregister"]