"""RotateAxisProperty: typed configuration surface for rotation-axis hosts.

Lives inside the RotateAxis module package: the PropertyGroup is the
configuration surface for ``RotateAxis*`` hosts (attached to every
``bpy.types.Object`` via ``Object.rotate_axis``). Kept separate from
``LinearAxisProperty`` so the LinearAxis module stays LinearAxis-only.

Defines a ``bpy.types.PropertyGroup`` (``RotateAxisProperty``) that is
attached to every ``bpy.types.Object`` via ``Object.rotate_axis`` when
:func:`register` runs.

Fields
------
``enabled``            : master toggle. ``False`` skips discovery.
``rotate_center``      : PointerProperty(Object). The static rotation
                         centre (cylinder/ring); its AABB centre is
                         the pivot.
``rotator``            : PointerProperty(Object). The moving rotator
                         body. The runtime rotates it around the
                         rotate centre's world pivot each tick; no
                         parent relationship with ``rotate_center``
                         is required.
``trigger_shim``       : PointerProperty(Object). Optional submesh used
                         for clean AABB overlap detection. May be
                         parented to the rotator (recommended) but
                         the runtime treats it as an independent
                         source object regardless.
``rotate_home_sensor`` : PointerProperty(Object). Homing sensor.
``rotate_pos_limit_sensor``: PointerProperty(Object). Positive limit.
``rotate_neg_limit_sensor``: PointerProperty(Object). Negative limit.
``rotate_stroke``      : FloatProperty. Informational travel range (deg).
``angular_speed``      : FloatProperty. Default angular velocity (deg/s).
``angular_home_speed`` : FloatProperty. Homing angular velocity (deg/s).
``rotate_axis_index``  : IntProperty. Local Euler component of the
                         RotateCenter: 0 = local X, 1 = local Y,
                         2 = local Z (default). Auto-detected from the
                         centre mesh's bounding box at discovery if left
                         at the default.

UI command fields
-----------------
``move_target_angle``    : FloatProperty. Target angle for the move_to
                           command; duration uses the runtime default.
``home_direction``       : IntProperty. ``-1`` (default) or ``+1``.
"""

import bpy


class RotateAxisProperty(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(default=True)

    # Host references.
    rotate_center: bpy.props.PointerProperty(type=bpy.types.Object)
    rotator: bpy.props.PointerProperty(type=bpy.types.Object)
    trigger_shim: bpy.props.PointerProperty(type=bpy.types.Object)

    # Sensors.
    rotate_home_sensor: bpy.props.PointerProperty(type=bpy.types.Object)
    rotate_pos_limit_sensor: bpy.props.PointerProperty(type=bpy.types.Object)
    rotate_neg_limit_sensor: bpy.props.PointerProperty(type=bpy.types.Object)

    # Motion parameters.
    rotate_stroke: bpy.props.FloatProperty(default=360.0)
    angular_speed: bpy.props.FloatProperty(
        name="Angular Speed",
        description="Default angular velocity (deg/s)",
        default=30.0,
    )
    angular_home_speed: bpy.props.FloatProperty(
        name="Homing Angular Speed",
        description="Homing angular velocity (deg/s)",
        default=10.0,
    )
    rotate_axis_index: bpy.props.IntProperty(
        name="Rotation Axis Index",
        description=(
            "Local Euler component of the RotateCenter that defines the "
            "rotation pivot. 0 = local X, 1 = local Y, 2 = local Z "
            "(default). Auto-detected from the centre mesh's bounding box "
            "at discovery if left at the default."
        ),
        default=2,
        min=0,
        max=2,
    )

    # UI-only command input fields.
    move_target_angle: bpy.props.FloatProperty(
        name="Move Target Angle",
        description="Target angle for the rotate move_to command (deg)",
        default=0.0,
    )
    home_direction: bpy.props.IntProperty(default=-1)


def register() -> None:
    """Register the PropertyGroup and attach it to ``bpy.types.Object``."""
    bpy.utils.register_class(RotateAxisProperty)
    bpy.types.Object.rotate_axis = bpy.props.PointerProperty(
        type=RotateAxisProperty
    )


def unregister() -> None:
    """Detach the PropertyGroup and unregister the class."""
    if hasattr(bpy.types.Object, "rotate_axis"):
        del bpy.types.Object.rotate_axis
    bpy.utils.unregister_class(RotateAxisProperty)