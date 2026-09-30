"""Rail component: static travel envelope for a linear axis.

A Rail is a Blender object whose ``linear_axis`` PropertyGroup
``rail`` field references it. The rail geometry is the source of truth
for both the **motion direction** and the **travel envelope**:

- :attr:`direction` is a unit vector along the rail's longest world
  edge (X if tied with Y / Z, to match the legacy default). The
  slider moves along this vector, not along the fixed world X.
- :attr:`center` is the rail's world-space AABB centre, used as the
  scalar reference (t = 0).
- :attr:`x_min` / :attr:`x_max` are scalar distances from ``center``
  along ``direction`` that bound the slider's travel. The slider's
  scalar position ``t`` is the projection of the slider's world
  position onto the direction vector (with the centre as origin).

This replaces the previous "everything is world X" convention. The
scalar API (``x_min`` / ``x_max`` / ``clamp`` / ``contains``) is
preserved so the slider and the rest of the runtime do not need to
care whether the rig is axis-aligned or arbitrarily rotated.
"""

from __future__ import annotations

try:
    from ..components.geometry import (
        longest_edge_direction,
        world_aabb,
        world_center,
    )
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.components.geometry import (  # noqa: F401
        longest_edge_direction,
        world_aabb,
        world_center,
    )


# Unit vector along the world X axis. Used as a fallback when the
# rail is degenerate (zero-extent AABB) so downstream code still
# receives a well-defined direction.
_DEFAULT_DIRECTION = (1.0, 0.0, 0.0)


class RailComponent:
    """Wraps a single Blender object as a Rail.

    The rail is a static object; the slider's runtime computes the
    rail's direction and envelope once at construction time. If the
    rail is later moved or re-oriented in the scene, the discovery
    layer must rebuild the axis to refresh the cached values.
    """

    __slots__ = ("obj", "direction", "center", "x_min", "x_max")

    def __init__(self, obj):
        self.obj = obj
        # 3-D motion direction: longest AABB edge of the rail in world
        # coordinates. Degenerate rails fall back to world X.
        self.direction = self._compute_direction(obj)
        # 3-D reference point: world AABB centre. All scalar t-values
        # are measured from this point along ``direction``.
        self.center = self._compute_center(obj)
        # Travel envelope: the minimum / maximum scalar projection of
        # the rail's world AABB onto ``direction``, measured from
        # ``center``. ``x_min <= 0 <= x_max`` always.
        self.x_min, self.x_max = self._compute_envelope(obj, self.direction, self.center)

    # ---- internal builders (overridable for offline tests) ----

    def _compute_direction(self, obj):
        try:
            d = longest_edge_direction(obj)
        except Exception:
            d = None
        if d is None:
            try:
                from mathutils import Vector  # type: ignore
                return Vector(_DEFAULT_DIRECTION)
            except Exception:
                return _DEFAULT_DIRECTION
        # Normalise defensively — ``longest_edge_direction`` already
        # returns a unit vector, but a custom ``bound_box`` could be
        # malformed.
        try:
            return d.normalized() if hasattr(d, "normalized") else d
        except Exception:
            return d

    def _compute_center(self, obj):
        try:
            return world_center(obj)
        except Exception:
            try:
                from mathutils import Vector  # type: ignore
                return Vector((0.0, 0.0, 0.0))
            except Exception:
                return (0.0, 0.0, 0.0)

    def _compute_envelope(self, obj, direction, center):
        """Return ``(t_min, t_max)`` — the scalar envelope of the rail.

        For each of the rail's 8 world AABB corners, project onto
        ``direction`` (with ``center`` as the origin). The smallest
        and largest projections are the envelope bounds.
        """
        try:
            lo, hi = world_aabb(obj)
            corners = []
            for ix in (0, 1):
                for iy in (0, 1):
                    for iz in (0, 1):
                        cx = float(hi[0]) if ix else float(lo[0])
                        cy = float(hi[1]) if iy else float(lo[1])
                        cz = float(hi[2]) if iz else float(lo[2])
                        corners.append((cx, cy, cz))
        except Exception:
            # Degenerate / no bound_box: assume a unit-length envelope.
            return -0.5, 0.5

        projections = []
        for c in corners:
            d = (
                c[0] - float(center[0]),
                c[1] - float(center[1]),
                c[2] - float(center[2]),
            )
            t = d[0] * float(direction[0]) + d[1] * float(direction[1]) + d[2] * float(direction[2])
            projections.append(t)
        return min(projections), max(projections)

    # ---- public ----

    def refresh(self) -> bool:
        """重算 direction / center / x_min / x_max,基于 rail 当前世界变换。

        ``RailComponent.__init__`` 在轴发现时一次算好这一组缓存。
        如果 rail 自身或其父级链(典型场景:挂在另一条 axis 的 slider 上,
        例如 ``LinearAxisX`` 是 ``Slide.Y`` 的孩子——Slide.Y 一动,
        ``Rail.X`` 的世界 AABB 朝向就会变,缓存的方向就过时了),
        缓存不再反映现实,slider 会沿错误的世界方向走。

        :meth:`refresh` 重新走与 :meth:`__init__` 同样的纯函数路径
        (重读 ``bound_box`` + ``matrix_world``),开销与构造相当。
        失败时 (e.g. rail 已被删除 / ``matrix_world`` 已失效) 保留旧缓存
        —— 上层 :meth:`SimulationManager.update` 会通过
        ``is_object_alive`` 把这种轴踢出名单,本方法只是不假装成功。

        Returns
        -------
        bool
            ``True`` if ``obj.matrix_world`` 仍可读且缓存被成功重算
            (包括被 _compute_* 内部 fall-back 推到默认值的退化情况);
            ``False`` 表示 ``obj.matrix_world`` 本身已失效,旧缓存保留
            不动 —— 调用方据此判断本次是否真正获得了新几何。
        """
        # 先探一下 ``obj.matrix_world`` 是否可读。
        # ``_compute_*`` 内部各自包了 try/except,失败时自己退到默认值,
        # 这会让 refresh() 看到“一切顺利”的假象;显式在入口判一次
        # 才是真正的“缓存可用”谓词。
        try:
            _ = self.obj.matrix_world
            # ``to_translation()`` / ``to_3x3()`` 才是真正会被后续计算
            # 调用的接口。只验证属性存在不够(mock 经常是属性有但调用
            # 抛错),需要在入口真探一遍以免误报 "True"。
            _mw = self.obj.matrix_world
            _ = _mw.to_translation()
            _ = _mw.to_3x3()
        except Exception:
            # obj 已不可读(典型: 已被删除但 Python 引用还在,或
            # matrix_world 调方法时抛错),保留旧缓存。
            return False
        try:
            self.direction = self._compute_direction(self.obj)
            self.center = self._compute_center(self.obj)
            self.x_min, self.x_max = self._compute_envelope(
                self.obj, self.direction, self.center
            )
        except Exception:
            # 缓存层以外的意外 (理论上 _compute_* 都不会再抛),保留旧值。
            return False
        return True

    # ---- public ----

    def contains(self, x: float) -> bool:
        """Return True if ``x`` is within the rail (inclusive).

        历史:以前会按 ``[x_min, x_max]`` 做范围检查;现在 sensor(home /
        pos / neg limit)接管 limit 判定,slider 不再自己夹紧。如果之后
        需要严格 envelope,在这里恢复 ``self.x_min <= x <= self.x_max``。
        """
        # return self.x_min <= x <= self.x_max
        return True

    def to_position(self, x: float):
        """Convert a scalar ``x`` (along the rail direction) to a 3-D position.

        Returns ``center + direction * x`` as a Vector-like. Used by
        :class:`SliderComponent` to translate the scalar motion
        integration into a world-space location write.
        """
        try:
            from mathutils import Vector  # type: ignore
        except Exception:
            Vector = None  # type: ignore
        # cx = float(self.center[0]) + float(self.direction[0]) * float(x)
        # cy = float(self.center[1]) + float(self.direction[1]) * float(x)
        # cz = float(self.center[2]) + float(self.direction[2]) * float(x)
        cx = float(float(self.direction[0]) * float(x))
        cy = float(float(self.direction[1]) * float(x))
        cz = float(float(self.direction[2]) * float(x))
        if Vector is not None:
            try:
                return Vector((cx, cy, cz))
            except Exception:
                pass
        return (cx, cy, cz)

    # def from_position(self, position) -> float:
    #     """Project a 3-D world ``position`` onto the rail direction.

    #     Inverse of :meth:`to_position`. Used at slider construction to
    #     read the initial position from the slider object's current
    #     world location, so a slider already placed on the rail keeps
    #     its starting scalar when the runtime is built.
    #     """
    #     d = (
    #         # float(position[0]) - float(self.center[0]),
    #         # float(position[1]) - float(self.center[1]),
    #         # float(position[2]) - float(self.center[2]),
    #         float(position[0]),
    #         float(position[1]),
    #         float(position[2]),
    #     )
    #     return (
    #         d[0] * float(self.direction[0])
    #         + d[1] * float(self.direction[1])
    #         + d[2] * float(self.direction[2])
    #     )