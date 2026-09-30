"""LinearAxisProperty: typed configuration surface for LinearAxis hosts.

Lives inside the LinearAxix module package: the PropertyGroup is the
configuration surface for LinearAxis hosts (attached to every
``bpy.types.Object`` via ``Object.linear_axis``). Rotation axes have
their own PropertyGroup (``modules/RotateAxis/component.py``) so the
LinearAxis surface stays LinearAxis-only.

Defines a ``bpy.types.PropertyGroup`` (``LinearAxisProperty``) that is
attached to every ``bpy.types.Object`` via ``Object.linear_axis`` when
:func:`register` runs. Mirrors the V0_2 reference framework's
component shape (``PointerProperty(Object)`` refs + ``FloatProperty``
tunables) but defined at module level so Blender 5.1's ``register_class``
``get_type_hints`` introspection path finds ``bpy`` in scope.

Fields
------
``enabled``        : master toggle. ``False`` skips discovery.
``rail``           : PointerProperty(Object). The static travel envelope.
``slider``         : PointerProperty(Object). The moving module.
``trigger_shim``   : PointerProperty(Object). Optional submesh used for
                     clean AABB overlap detection; must be parented to
                     the slider.
``home_sensor``    : PointerProperty(Object). Triggers homing termination.
``pos_sensor``     : PointerProperty(Object). back_limit / positive limit.
``neg_sensor``     : PointerProperty(Object). front_limit / negative limit.
``speed``          : FloatProperty. Default velocity (mm/s).
``home_speed``     : FloatProperty. Homing velocity (mm/s).

UI command fields
-----------------
``move_target_x``  : FloatProperty. Target X for the move_to command (mm);
                     duration is computed from ``speed``.
``home_direction`` : IntProperty. ``-1`` (default) or ``+1`` for homing.

单位约定
--------
场景按 mm 工作流搭建（Unit Scale = 0.001，即 1 Blender 单位 = 1 mm，
几何尺寸直接用 mm 数值），因此上面所有长度/速度字段的数值直接就是
mm / mm·s⁻¹，与插件内部使用的 Blender 单位在数值上相等，无需换算。
若在默认米制场景（1 BU = 1 m）使用本面板，请自行注意数值差异。
"""

import bpy


class LinearAxisProperty(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(default=True)
    rail: bpy.props.PointerProperty(type=bpy.types.Object)
    slider: bpy.props.PointerProperty(type=bpy.types.Object)
    trigger_shim: bpy.props.PointerProperty(type=bpy.types.Object)
    home_sensor: bpy.props.PointerProperty(type=bpy.types.Object)
    pos_sensor: bpy.props.PointerProperty(type=bpy.types.Object)
    neg_sensor: bpy.props.PointerProperty(type=bpy.types.Object)
    speed: bpy.props.FloatProperty(default=0.5)
    home_speed: bpy.props.FloatProperty(default=0.1)

    # UI-only command input fields consumed by ``linear_axis.ui``.
    move_target_x: bpy.props.FloatProperty(default=0.0)
    home_direction: bpy.props.IntProperty(default=-1)


def register() -> None:
    """Register the PropertyGroup and attach it to ``bpy.types.Object``."""
    bpy.utils.register_class(LinearAxisProperty)
    bpy.types.Object.linear_axis = bpy.props.PointerProperty(
        type=LinearAxisProperty
    )


def unregister() -> None:
    """Detach the PropertyGroup and unregister the class."""
    if hasattr(bpy.types.Object, "linear_axis"):
        del bpy.types.Object.linear_axis
    bpy.utils.unregister_class(LinearAxisProperty)