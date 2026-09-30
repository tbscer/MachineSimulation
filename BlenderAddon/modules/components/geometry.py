"""AABB / world-bounds / overlap utilities.

Pure functions that take any object exposing ``bound_box`` (iterable of
8 corner coordinate sequences) and ``matrix_world`` (a 4x4 transform
supporting the ``@`` operator with a ``Vector``-like). Works inside
Blender against real ``bpy.types.Object`` and in offline tests against a
mock that fakes ``Vector``.

The module never imports ``bpy`` itself; the Blender dependency is only
on the objects passed in. This keeps unit tests trivial.

Conventions
-----------
- All functions operate in **world** coordinates.
- Overlap on an axis uses strict ``<``/``>`` by default (boundary
  touching does not count as overlap). Pass ``touching=True`` to make
  boundary contact count.
- ``world_aabb`` returns a tuple ``(min_vec, max_vec)`` where each
  element supports ``.x``/``.y``/``.z`` indexing.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence, Tuple


# ---- AABB ----

def _transform_corners(bound_box: Iterable[Sequence[float]],
                       matrix_world) -> list:
    """Transform 8 local-space corners to world space via ``matrix_world``.

    Uses duck-typed ``Vector(corner)``: any callable accepting an iterable
    of 3 floats and returning an object supporting ``@`` returns a new
    transformed object. The Blender ``mathutils.Vector`` qualifies; the
    test mock supplies a tiny stand-in.
    """
    world_corners = []
    for corner in bound_box:
        v = _make_vector(corner)
        world_corners.append(matrix_world @ v)
    return world_corners


def _make_vector(coords):
    """Build a Vector-like object.

    Tries ``mathutils.Vector`` first; falls back to a small list-based
    stand-in for offline tests.
    """
    try:
        from mathutils import Vector  # type: ignore
        return Vector(coords)
    except Exception:
        return _ListVector(coords)


class _ListVector:
    """Minimal stand-in for ``mathutils.Vector`` (3-tuple only)."""

    __slots__ = ("_xyz",)

    def __init__(self, coords):
        c = tuple(coords)
        if len(c) != 3:
            raise ValueError("_ListVector only supports 3 components")
        self._xyz = (float(c[0]), float(c[1]), float(c[2]))

    @property
    def x(self):
        return self._xyz[0]

    @property
    def y(self):
        return self._xyz[1]

    @property
    def z(self):
        return self._xyz[2]

    def __getitem__(self, idx):
        return self._xyz[idx]

    def __iter__(self):
        return iter(self._xyz)

    def __repr__(self):
        return f"_ListVector({self._xyz})"


def _component_min_max(world_corners, axis: int) -> Tuple[float, float]:
    lo = min(c[axis] for c in world_corners)
    hi = max(c[axis] for c in world_corners)
    return lo, hi


def world_aabb(obj, depsgraph=None) -> Tuple[object, object]:
    """Return ``(min_vec, max_vec)`` of an object's world-space AABB.

    ``obj`` must expose ``bound_box`` (8 corners in local space) and
    ``matrix_world``. ``depsgraph`` is accepted for forward
    compatibility with evaluated-geometry workflows but currently
    unused here (the bound_box already reflects evaluated geometry for
    the simple mesh objects this addon targets).
    """
    corners = _transform_corners(obj.bound_box, obj.matrix_world)

    def _vec(lo, hi):
        try:
            from mathutils import Vector  # type: ignore
            return Vector(lo), Vector(hi)
        except Exception:
            return _ListVector(lo), _ListVector(hi)

    lo_xyz = (
        min(c[0] for c in corners),
        min(c[1] for c in corners),
        min(c[2] for c in corners),
    )
    hi_xyz = (
        max(c[0] for c in corners),
        max(c[1] for c in corners),
        max(c[2] for c in corners),
    )
    lo, hi = _vec(lo_xyz, hi_xyz)
    return lo, hi


def world_x_range(obj, depsgraph=None) -> Tuple[float, float]:
    """Return ``(xmin, xmax)`` of an object's world-space AABB on X only.

    Cheaper than ``world_aabb`` when only the X axis matters (the common
    case for a 1-D linear axis).
    """
    corners = _transform_corners(obj.bound_box, obj.matrix_world)
    return _component_min_max(corners, 0)


def longest_edge_direction(obj, depsgraph=None):
    """Return a unit vector along the object's longest world-space AABB edge.

    The linear axis's :class:`RailComponent` uses this to derive the
    1-D motion direction: a long horizontal rail produces ``(1, 0, 0)``;
    a rail rotated 45° around Z produces the rotated unit vector; a
    rail aligned with local Y produces ``(0, 1, 0)``.

    Tie-breaking
    ------------
    When two or three dimensions are equal in length the function
    prefers X, then Y, then Z. This is deterministic and matches the
    long-standing default behaviour of the addon (the original code
    always assumed motion along world X).

    Degenerate case
    ---------------
    A zero-extent rail (a single point or collapsed mesh) returns the
    world X axis ``(1, 0, 0)`` so downstream code has a well-defined
    direction.

    Returns a Vector-like (3 components, ``.x`` / ``.y`` / ``.z``).
    """
    corners = _transform_corners(obj.bound_box, obj.matrix_world)
    extents = (
        max(c[0] for c in corners) - min(c[0] for c in corners),
        max(c[1] for c in corners) - min(c[1] for c in corners),
        max(c[2] for c in corners) - min(c[2] for c in corners),
    )
    # Tie-break X > Y > Z (deterministic; matches the legacy X-axis default).
    axis = 0
    if extents[1] > extents[axis]:
        axis = 1
    if extents[2] > extents[axis]:
        axis = 2
    # Build the unit vector along the chosen axis. Avoids importing
    # ``mathutils`` for the trivial case so the helper stays cheap.
    vec = (0.0, 0.0, 0.0)
    vec = vec[:axis] + (1.0,) + vec[axis + 1:]
    return _make_vector(vec)


def world_center(obj, depsgraph=None):
    """Return the world-space centre of an object's AABB as a Vector-like.

    The shared 3-D analogue of :func:`world_x_range`: used by rotational
    modules to locate rotation centres and sensor centres.
    """
    lo, hi = world_aabb(obj, depsgraph=depsgraph)
    return _make_vector((
        (float(lo[0]) + float(hi[0])) / 2.0,
        (float(lo[1]) + float(hi[1])) / 2.0,
        (float(lo[2]) + float(hi[2])) / 2.0,
    ))


def end_face_center(source_obj, target_obj=None, depsgraph=None):
    """Return the world-space centre of one of the source's end-faces.

    The "length axis" of the source is its longest world-space
    dimension (typically the cylinder/cone axis for a trigger shim).
    The two end-face centres are the source's world AABB centre
    offset by ``±half_length`` along that axis.

    When ``target_obj`` is provided, returns the end-face centre that
    is closer to the target — i.e. the "approaching" face, which is
    the natural interpretation for a trigger shim sweeping toward a
    sensor. When ``target_obj`` is ``None``, returns the ``+`` axis
    end.

    Falls back to the source's world AABB centre for degenerate
    (zero-length) sources, and to ``None`` only when even that fails
    (caller should treat ``None`` as "not inside the sensor").
    """
    try:
        lo, hi = world_aabb(source_obj, depsgraph=depsgraph)
        extents = (
            float(hi[0]) - float(lo[0]),
            float(hi[1]) - float(lo[1]),
            float(hi[2]) - float(lo[2]),
        )
        longest = 0
        if extents[1] > extents[longest]:
            longest = 1
        if extents[2] > extents[longest]:
            longest = 2
        # Degenerate case: no single dominant axis (cube, sphere,
        # etc.). The "end-face" concept has no meaning, so fall back
        # to the source's world AABB centre.
        if extents[longest] <= 1e-12:
            return world_center(source_obj, depsgraph=depsgraph)
        # Also treat a near-tie as degenerate: if another axis is
        # within 1% of the longest, the object isn't really elongated
        # along a single axis and the end-face pick would be arbitrary.
        for i in range(3):
            if i != longest and extents[i] >= 0.99 * extents[longest]:
                return world_center(source_obj, depsgraph=depsgraph)
        half_length = extents[longest] / 2.0
        center = world_center(source_obj, depsgraph=depsgraph)
        offsets = [0.0, 0.0, 0.0]
        offsets[longest] = half_length
        plus = _make_vector((
            float(center[0]) + offsets[0],
            float(center[1]) + offsets[1],
            float(center[2]) + offsets[2],
        ))
        offsets[longest] = -half_length
        minus = _make_vector((
            float(center[0]) + offsets[0],
            float(center[1]) + offsets[1],
            float(center[2]) + offsets[2],
        ))
        if target_obj is None:
            return plus
        try:
            tgt = world_center(target_obj, depsgraph=depsgraph)
        except Exception:
            return plus
        d_plus = ((float(plus[0]) - float(tgt[0])) ** 2
                  + (float(plus[1]) - float(tgt[1])) ** 2
                  + (float(plus[2]) - float(tgt[2])) ** 2)
        d_minus = ((float(minus[0]) - float(tgt[0])) ** 2
                   + (float(minus[1]) - float(tgt[1])) ** 2
                   + (float(minus[2]) - float(tgt[2])) ** 2)
        return minus if d_minus < d_plus else plus
    except Exception:
        try:
            return world_center(source_obj, depsgraph=depsgraph)
        except Exception:
            return None


def aabb_contains_point(lo, hi, p, touching: bool = True) -> bool:
    """Return True iff point ``p`` lies inside the AABB ``(lo, hi)``.

    All arguments are 3-component sequences. ``touching=True`` (default)
    counts points exactly on a face as inside.
    """
    if touching:
        return all(float(lo[i]) <= float(p[i]) <= float(hi[i]) for i in range(3))
    return all(float(lo[i]) < float(p[i]) < float(hi[i]) for i in range(3))


# ---- 3-D transform helpers (rotational modules) ----

def local_point(matrix_world, world_point):
    """Transform a world-space point into an object's local frame.

    Assumes a rigid transform (rotation + translation, unit scale): the
    inverse is computed as ``R^T (p - t)`` from the upper 3x3 and
    translation column, which only needs row/column indexing — no
    ``inverted()`` — so it works with both ``mathutils.Matrix`` and the
    offline test mock.
    """
    m = matrix_world
    px, py, pz = (float(world_point[0]) - float(m[0][3]),
                  float(world_point[1]) - float(m[1][3]),
                  float(world_point[2]) - float(m[2][3]))
    # R^T @ d: rows of R^T are columns of R.
    return _make_vector((
        float(m[0][0]) * px + float(m[1][0]) * py + float(m[2][0]) * pz,
        float(m[0][1]) * px + float(m[1][1]) * py + float(m[2][1]) * pz,
        float(m[0][2]) * px + float(m[1][2]) * py + float(m[2][2]) * pz,
    ))


def world_point(matrix_world, local_point_):
    """Transform a local-frame point to world space via ``matrix_world``."""
    v = _make_vector(local_point_)
    return matrix_world @ v


def normalize_angle_deg(angle: float) -> float:
    """Wrap an angle in degrees into the half-open range ``(-180, 180]``.

    Convention: ``-180`` folds to ``+180`` (the half-open boundary on
    the positive side wins). Industrial controllers and ``math.atan2``
    both use this convention so the two views of "the same angle"
    always produce the same scalar.
    """
    a = float(angle) % 360.0
    if a > 180.0:
        a -= 360.0
    return a


def wrap_angle_deg(angle: float) -> float:
    """Wrap an angle in degrees into the range ``[0, 360)``.

    Useful when monotonic counters or "home is at 0°" conventions are
    preferred over signed angles.
    """
    return float(angle) % 360.0


def angle_from_xy(x: float, y: float) -> float:
    """Return the angle (degrees) of the XY-plane vector ``(x, y)``.

    Convention: 0° = +X, 90° = +Y, increasing counter-clockwise. The
    ``math.atan2`` call wraps to ``(-pi, pi]`` and we convert to degrees.
    Independent of the world frame: callers pass the offset relative
    to whatever centre they care about (typically the RotateCenter).
    """
    return math.degrees(math.atan2(float(y), float(x)))


def angle_delta_deg(a_from: float, a_to: float) -> float:
    """Signed shortest-arc delta from ``a_from`` to ``a_to`` in degrees.

    Result lies in ``(-180, 180]``. ``+90`` means ``a_to`` is 90°
    counter-clockwise of ``a_from``. Used by the rotate slider for
    limit-sensor polarity and p2p velocity computation.
    """
    return normalize_angle_deg(float(a_to) - float(a_from))


# ---- 1-D overlap ----

def x_overlap(a_min: float, a_max: float,
              b_min: float, b_max: float,
              touching: bool = False) -> bool:
    """Return True if two X intervals overlap.

    ``a_min <= a_max`` and ``b_min <= b_max`` are assumed; no internal
    sorting. By default uses strict ``<``/``>``: intervals that merely
    touch at one point do **not** count as overlapping. Set
    ``touching=True`` to include boundary contact.
    """
    if touching:
        return (a_min <= b_max) and (a_max >= b_min)
    return (a_min < b_max) and (a_max > b_min)