"""Per-scene unit conversion helpers shared by axis / axis_ops.

The addon's UI operator and ``axis_ops.send_command`` accept
length / speed values in **millimetres**. The runtime works in
**Blender Units** (the value stored in ``axis_cmd_*`` custom
properties and integrated by :class:`SliderComponent.update`).
These helpers bridge the two so the runtime does not need to
know what unit the user is typing in.

Conversion factor
-----------------
``unit_settings.scale_length`` is "metres per Blender Unit" — e.g.
``1.0`` means ``1 BU = 1 m`` (Blender's default). To go from mm to
BU we divide by ``scale_length * 1000``:

    BU = mm * 0.001 / scale_length

A scene whose length-unit is set to "Millimeters" but whose
scale is left at ``1.0`` is therefore "1 BU = 1 m" with display in
mm — that is the configuration we explicitly support here,
because it lets the user keep Blender's default Unit Scale and
still have the operator panel feel like mm.
"""

from __future__ import annotations


def scene_linear_mm_to_bu_factor() -> float:
    """Multiplier that converts a length in **mm** into **BU**.

    Falls back to ``0.001`` (i.e. assumes ``1 BU = 1 mm``) when the
    scene is unreachable — headless tests, scripts run before
    Blender initialised the data block, etc.
    """
    try:
        import bpy  # type: ignore
        scale = float(bpy.context.scene.unit_settings.scale_length)
    except Exception:
        return 0.001
    if scale <= 0.0:
        return 0.001
    return 0.001 / scale


def scene_linear_bu_to_mm_factor() -> float:
    """Inverse of :func:`scene_linear_mm_to_bu_factor`.

    Used by :meth:`LinearAxis._write_state_props` and
    :meth:`LinearAxis.snapshot` so the ``axis_*`` custom properties
    and the RPC state-push payload are reported in mm.
    """
    try:
        import bpy  # type: ignore
        scale = float(bpy.context.scene.unit_settings.scale_length)
    except Exception:
        return 1000.0
    if scale <= 0.0:
        return 1000.0
    return scale * 1000.0