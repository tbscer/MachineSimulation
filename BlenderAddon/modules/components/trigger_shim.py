"""TriggerShim component: passive mesh attached to a Slider.

The shim has no logic of its own — it moves because it is parented to
the Slider. Its only job is to be available for AABB overlap detection
against Sensors.

The class captures the parent-Slider reference at construction time and
re-checks the parent relationship on each :meth:`validate` call so a
misconfigured scene fails loudly instead of silently never triggering.
"""

from __future__ import annotations


class TriggerShimComponent:
    """Wraps a single Blender object as a TriggerShim."""

    __slots__ = ("obj", "slider_obj")

    def __init__(self, obj, slider_obj):
        self.obj = obj
        self.slider_obj = slider_obj

    # ---- public ----

    def validate(self) -> None:
        """Raise ``ValueError`` if the shim is not parented to the slider.

        Called once per discovery pass, not per tick, because Blender
        parent relationships rarely change at runtime.
        """
        if self.obj.parent is not self.slider_obj:
            raise ValueError(
                "TriggerShim {!r} must be parented to Slider {!r}, "
                "found parent={!r}".format(
                    self.obj.name, self.slider_obj.name,
                    None if self.obj.parent is None else self.obj.parent.name,
                )
            )