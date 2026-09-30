"""RotateCenter component: a **static pivot marker** for a rotation axis.

Conceptually, the RotateCenter is *not* a moving body. It is a
non-visual (or merely decorative) reference object that exists for one
purpose: to define the **rotation pivot** for the rig. The runtime
uses the centre's world AABB centre as the pivot point and the
centre's local axis (selected via ``cfg.rotate_axis_index``,
transformed to world space through ``matrix_world``) as the rotation
axis. The centre's own ``rotation_euler`` is **never written by the
runtime** — the rotator's angle is tracked internally and translated
each tick into a world-matrix rotation around the centre's pivot.
No parenting between the Rotator and the RotateCenter is required.

Workflow
--------
1. Place or designate a Blender object (a cylinder, a ring, or even
   an empty) at the desired rotation pivot. The world AABB centre of
   this object is the rotation pivot.
2. Pick the **local axis** of this object that aligns with the
   cylinder/shaft's length direction. That local axis (transformed to
   world space) becomes the rotation axis.
3. Place the orbiting body (``Rotator``) anywhere in the scene — no
   parenting required. The runtime rotates it around the centre's
   world pivot on each tick.

The RotateCenter has no logic of its own at runtime — it just owns the
pivot reference (``pivot_world``) and exposes the axis selection
(``axis_index``). The :class:`RotateAxisRuntime` uses these to
translate the rotator's internal angle into a world-matrix rotation
each tick.

``apply_rotation`` / ``read_angle`` are kept on the component as a
generic Euler write/read helper (used by the standalone unit tests
for the component) but the runtime no longer drives them — angle
bookkeeping lives on the rotator.

Axis selection
--------------
``axis_index`` selects which **local** Euler component of the centre
is the rotation axis. ``0`` = X, ``1`` = Y, ``2`` = Z (default). The
local axis is intentionally chosen, not the world axis — the
rotation pivot lives on the centre's own mesh, and any rotation is
applied to that local axis (which is then transformed to world space
for the visual write).

The discovery layer auto-detects the correct axis index from the
centre mesh's local AABB extent if ``rotate_axis_index`` is left at
its default (``2``): the longest local dimension is assumed to be the
cylinder's length axis.
"""

from __future__ import annotations

import math

try:
    from ..components.geometry import world_aabb, world_center
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.components.geometry import world_aabb, world_center


AXIS_X = 0
AXIS_Y = 1
AXIS_Z = 2

VALID_AXES = (AXIS_X, AXIS_Y, AXIS_Z)


class RotateCenterComponent:
    """Wraps a Blender object as the rotation pivot of a rotation axis."""

    __slots__ = ("obj", "axis_index", "_pivot", "_pivot_initialized")

    def __init__(self, obj, *, axis_index: int = AXIS_Z):
        self.obj = obj
        if axis_index not in VALID_AXES:
            axis_index = AXIS_Z
        self.axis_index = int(axis_index)
        # Cached world-space pivot. Refreshed on demand by ``refresh_pivot``.
        self._pivot = None
        self._pivot_initialized = False

    # ---- public ----

    @property
    def pivot_world(self):
        """Return the world-space pivot as a Vector-like (cached).

        Returns ``None`` if the underlying object cannot produce an
        AABB (e.g. outside a Blender context during unit tests). The
        runtime treats ``None`` as a soft error and disables motion.
        """
        if not self._pivot_initialized:
            self.refresh_pivot()
        return self._pivot

    def refresh_pivot(self) -> bool:
        """Recompute the cached pivot from the object's current world AABB.

        Returns ``True`` if a pivot is available. Cheap enough to call
        every tick: the geometry helpers are pure functions over the
        object's current ``matrix_world`` and ``bound_box``.
        """
        try:
            lo, hi = world_aabb(self.obj)
            pivot = world_center(self.obj)
            # ``pivot`` already encodes the centre, but recompute from
            # the fresh AABB so the cache matches.
            pivot = type(pivot)(((
                float(lo[0]) + float(hi[0])) / 2.0,
                (float(lo[1]) + float(hi[1])) / 2.0,
                (float(lo[2]) + float(hi[2])) / 2.0,
            ))
            self._pivot = pivot
            self._pivot_initialized = True
            return True
        except Exception:
            self._pivot = None
            self._pivot_initialized = True
            return False

    def apply_rotation(self, angle_deg: float) -> bool:
        """Rotate the centre by ``angle_deg`` around its axis.

        Writes the rotation into the object's Euler on the component
        selected by ``axis_index``. Other Euler components are
        preserved. Returns True if the write succeeded.

        Blender's ``rotation_euler`` stores **radians**, but the rest
        of the rotate-axis pipeline (velocity, integration, sensors)
        works in degrees. This method is the conversion point:
        ``angle_deg`` → radians on write.
        """
        try:
            euler = self.obj.rotation_euler
            angle_rad = math.radians(float(angle_deg))
            if self.axis_index == AXIS_X:
                euler.x = angle_rad
            elif self.axis_index == AXIS_Y:
                euler.y = angle_rad
            else:
                euler.z = angle_rad
            return True
        except Exception:
            return False

    def read_angle(self) -> float:
        """Return the current rotation angle (degrees) along the configured axis.

        Mirror of :meth:`apply_rotation`: reads ``rotation_euler`` in
        radians and converts to degrees so the runtime's degree-based
        state machine can integrate without unit mismatch.
        """
        try:
            euler = self.obj.rotation_euler
            if self.axis_index == AXIS_X:
                return math.degrees(float(euler.x))
            if self.axis_index == AXIS_Y:
                return math.degrees(float(euler.y))
            return math.degrees(float(euler.z))
        except Exception:
            return 0.0