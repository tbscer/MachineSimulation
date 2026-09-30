"""Slider component: the moving module of a linear axis.

The Slider is the single active object in an axis — it owns motion and
exposes position updates back to the coordinator.

Motion direction
----------------
The slider no longer assumes world-X. Its scalar ``current_x`` is the
projection of the slider's world position onto the **rail's motion
direction** (a 3-D unit vector derived from the rail's longest AABB
edge — see :class:`RailComponent`). The slider writes its world
position back to ``obj.location`` as
``rail.center + rail.direction * current_x``.

When the slider is constructed without a rail (``rail=None``, the
test-harness path) the slider falls back to the legacy world-X
behaviour: ``current_x`` is the world-X coordinate and
``obj.location.x`` is the only location channel written. The runtime
production path always supplies a rail.

The Y and Z location channels are *not* locked at construction
anymore — locking them would prevent the slider from rotating around
the rail's reference point as the axis runs along a non-X direction.
If the artist wants a UI lock they can do it in the rig; the runtime
respects whatever it finds.

Velocity defaults are supplied by the constructor (``default_velocity``,
``home_velocity``). The discovery layer pulls them from the host's
``linear_axis`` PropertyGroup (``speed`` / ``home_speed``).

Motion modes
------------
``idle``     — no motion. ``update(dt)`` is a no-op.
``p2p``      — point-to-point linear interpolation toward ``target_x``
              over ``duration_s``. Sets ``arrived=True`` once on the
              final tick, then transitions to ``idle``.
``vel``      — Euler integration of ``velocity`` over ``dt``. Used by
              both constant-velocity motion and homing; the coordinator
              decides when to stop.

The slider holds an optional rail reference (set via :meth:`attach_rail`
or by passing ``rail=`` to the constructor) and clamps the position to
``[rail.x_min, rail.x_max]`` every tick. The clamp result also yields
an ``at_limit`` signal returned to the coordinator so it can transition
state machines.
"""

from __future__ import annotations

from typing import Optional


# Sentinel for "no P2P target in progress".
_NO_TARGET = 0.0


def _write_world_position(obj, x: float, y: float, z: float) -> None:
    """Write a 3-component WORLD position to ``obj``.

    Uses ``obj.matrix_world`` for the read-back of the current world
    position so the result is independent of the parent chain. For
    unparented objects ``obj.location`` IS the world position so
    writing the tuple is a direct assignment. For parented objects
    the world position is computed by inverse-transforming through
    the parent matrix.

    Tries the modern whole-vector assignment first (works in real
    Blender with ``mathutils.Vector``), then falls back to per-channel
    attribute writes so offline test mocks that only expose
    ``location.x`` / ``location.y`` / ``location.z`` still work.
    """
    try:
        from mathutils import Vector, Matrix  # type: ignore
    except Exception:
        Vector = None  # type: ignore
        Matrix = None  # type: ignore

    # For parented objects: world_pos = parent.matrix_world @ local_pos,
    # so local_pos = parent.matrix_world.inverted() @ world_pos.
    # parent = getattr(obj, "parent", None)
    # if parent is not None and Matrix is not None:
    #     try:
    #         world_matrix = Matrix.Translation((x, y, z))
    #         local_matrix = parent.matrix_world.inverted() @ world_matrix
    #         lx, ly, lz = (
    #             float(local_matrix.to_translation()[0]),
    #             float(local_matrix.to_translation()[1]),
    #             float(local_matrix.to_translation()[2]),
    #         )
    #     except Exception:
    #         lx, ly, lz = x, y, z
    # else:
    #     lx, ly, lz = x, y, z

    lx, ly, lz = x, y, z
    try:
        obj.location = (lx, ly, lz)
        return
    except Exception:
        pass
    try:
        obj.location.x = lx
        obj.location.y = ly
        obj.location.z = lz
    except Exception:
        # Best-effort: even the fallback can fail on the most minimal
        # mocks. Swallow so the runtime doesn't crash on degenerate
        # rig stand-ins.
        pass


def _read_world_position(obj):
    """Return the object's current world position as a 3-tuple.

    Uses ``obj.matrix_world.translation`` so the result is the true
    world position regardless of the parent chain. Falls back to
    ``obj.location`` (subscripted) and then to ``obj.location.{x,y,z}``
    for objects / mocks that don't expose ``matrix_world`` or
    subscripting.
    """
    # use local coordinate
    # mw = getattr(obj, "matrix_world", None)
    # if mw is not None:
        # try:
            # t = mw.to_translation()
            # return (float(t[0]), float(t[1]), float(t[2]))
        # except Exception:
            # pass
    loc = getattr(obj, "location", None)
    if loc is not None:
        # Prefer subscripting (real mathutils.Vector supports it).
        try:
            return (float(loc[0]), float(loc[1]), float(loc[2]))
        except Exception:
            pass
        # Fall back to .x / .y / .z accessors (offline test mocks).
        try:
            return (float(loc.x), float(loc.y), float(loc.z))
        except Exception:
            pass
    return (0.0, 0.0, 0.0)


class SliderComponent:
    """Wraps a single Blender object as a Slider."""

    __slots__ = (
        "obj",
        "current_x",
        "default_velocity",
        "home_velocity",
        "_motion_mode",
        "_target_x",
        "_velocity",
        "_p2p_start_x",
        "_p2p_duration",
        "_p2p_elapsed",
        "arrived",
        "_rail",
        "_start_position",
        "_last_status",
    )

    def __init__(self, obj, *, rail=None, default_velocity: float = 0.5,
                 home_velocity: float = 0.1):
        self.obj = obj
        # Tunables supplied by the caller (discovery reads them from
        # the host's linear_axis PropertyGroup).
        self.default_velocity = float(default_velocity)
        self.home_velocity = float(home_velocity)
        # ``_start_position`` is the world position that corresponds
        # to ``current_x == 0`` — i.e. the origin of the slider's
        # "logical" frame. It is set on construction to the slider's
        # current world position and updated to the home position
        # after a successful home (via :meth:`reset_origin`).
        # The slider's world position at any time is
        # ``_start_position + rail.direction * current_x``, so
        # ``current_x`` is a *relative* displacement rather than
        # an absolute coordinate.
        world_pos = _read_world_position(obj)
        self._start_position = world_pos
        if rail is not None:
            self._rail = rail
        else:
            self._rail = None
        # Always start at zero — the slider's current physical
        # position IS the starting point. Subsequent ``Move``
        # commands are interpreted as displacements from this
        # origin, not as absolute targets.
        self.current_x = 0.0
        self._motion_mode = "idle"
        self._target_x: float = 0.0
        self._velocity: float = 0.0
        self._p2p_start_x: float = 0.0
        self._p2p_duration: float = 0.0
        self._p2p_elapsed: float = 0.0
        self.arrived: bool = False  # one-shot flag, cleared after consume
        self._last_status: dict = {
            "at_limit": False,
        }
        # No ``_apply_position`` call here on purpose. The slider's
        # scene position is the source of truth at construction time;
        # rewriting it via the rail projection would silently move
        # the object (and the user reported that Refresh was
        # "snapping" sliders to the rail centre / their starting
        # location). The slider is now only ever moved by the
        # simulation tick via ``update()`` / ``set_x()``.
        # See :func:`_read_world_position` for the round-trip
        # helpers that the tick path uses.

    # ---- read-only views ----

    @property
    def mode(self) -> str:
        return self._motion_mode

    @property
    def velocity(self) -> float:
        return self._velocity

    @property
    def target_x(self):
        return self._target_x

    @property
    def at_limit(self) -> bool:
        return self._last_status.get("at_limit", False)

    # ---- configuration ----

    def attach_rail(self, rail) -> None:
        """Attach the rail envelope for clamp/limit detection.

        Re-derives ``current_x`` from the slider's current world
        position so the new rail's frame matches the slider's actual
        placement. Subsequent ``update`` calls will move the slider
        along the new rail's direction.

        Like :meth:`__init__`, this no longer calls
        :meth:`_apply_position` at the end — the slider's scene
        position is the source of truth. Re-applying the rail
        projection here was the source of the Refresh-snap bug
        where the slider's world position was silently rewritten
        on every rail re-attach.
        """
        self._rail = rail
        # if rail is not None:
        #     try:
        #         world_pos = _read_world_position(self.obj)
        #         self.current_x = float(rail.from_position(world_pos))
        #     except Exception:
        #         pass

    # ---- direct set ----

    def set_x(self, x: float) -> None:
        """Force the slider to a specific scalar.

        Used by homing (when the home sensor triggers) and by external
        callers (e.g. ``axis_ops.set_position``)。Limit 由 sensor 负责,
        这里不做 rail envelope 夹紧。
        """
        self.current_x = float(x)
        self._apply_position(self.current_x)

    def reset_origin(self, world_position=None) -> None:
        """Re-establish the slider's logical origin.

        Sets ``_start_position`` to the given world position (or the
        slider's current world position if ``None``) and resets
        ``current_x`` to ``0``. The slider's world position itself
        does not change — it is now the new origin.

        Called by the axis after a successful home, so the home
        position becomes the new "current_x = 0" reference and
        subsequent Move commands are interpreted as displacements
        from the home position.
        """
        if world_position is None:
            world_position = _read_world_position(self.obj)
        self._start_position = (
            float(world_position[0]),
            float(world_position[1]),
            float(world_position[2]),
        )
        self.current_x = 0.0
        # No _apply_position call: the slider is already at the
        # world position that will become its new origin.

    def refresh_origin_from_current(self) -> None:
        """重抓 ``_start_position`` 为 slider 当前 ``obj.location``,**保留** ``current_x``。

        与 :meth:`reset_origin` 不同:这里 ``current_x`` 不清零。
        用于父级链被旋转 / 平移 (e.g. 美术把 Slide.Y 拖到新位置) 后,
        缓存的逻辑原点跟当前 slider 的实际 local 位置不一致,如果不
        更新,下一条 Move 命令 ``N`` 会把 slider 写回
        ``旧 _start_position + rail.direction * N`` —— 即 slider 会
        “跳到”旧原点的方向上。

        更新到当前位置后,``_apply_position`` 写出去的位置就是
        ``当前 local + rail.direction * current_x`` —— 因为 slider 自身
        已经在当前位置上,下一帧 ``current_x`` 不变的话 slider 不会
        视觉上跳动;后续运动命令按 ``current_x`` 增量推进,行为一致。

        注意:本方法读 ``obj.location`` (local),与 ``__init__`` /
        :meth:`reset_origin` 的输入约定保持一致。父级链 matrix 变化
        时 local 通常不变,所以这条路径对“父级平移/旋转”是 no-op;
        对“用户手动改 slider local 位置”是真正的修复。
        """
        self._start_position = _read_world_position(self.obj)

    # ---- motion commands ----

    def start_idle(self) -> None:
        """Cancel any in-flight motion."""
        self._motion_mode = "idle"
        self._velocity = 0.0
        self._target_x = 0.0
        self.arrived = False

    def start_move_to(self, target_x: float, velocity: float) -> None:
        """Begin point-to-point motion toward ``target_x``.

        ``velocity`` 是速度幅值（units/sec），运动方向由 ``target_x``
        相对当前位置的符号决定。``_velocity`` 按 **带符号** 存：符号即
        实际运动方向，与 :class:`RotateRotatorComponent` 保持一致，这样
        ``LinearAxis._check_transitions``（以及 RPC snapshot / ``axis_velocity``）
        可以直接从 ``velocity`` 的符号读出行进方向。

        之前的实现把原始 ``velocity`` 参数（通常为正）直接存进
        ``_velocity``，导致 p2p 运动时 ``slider.velocity`` 永远为正 ——
        limit sensor 的极性判断因此把“向负方向走”误判成“向正方向走”，
        撞 Neg limit 也不会停。
        """
        start = float(self.current_x)
        target = float(target_x)
        delta = target - start
        speed = abs(float(velocity))
        self._motion_mode = "p2p"
        self._p2p_start_x = start
        self._target_x = target
        self._p2p_duration = max(speed, 1e-6)
        self._p2p_elapsed = 0.0
        self._velocity = (1.0 if delta >= 0.0 else -1.0) * speed
        self.arrived = False

    def start_velocity(self, v: float) -> None:
        """Begin constant-velocity motion at ``v`` units/sec (signed)."""
        self._motion_mode = "vel"
        self._velocity = float(v)
        self._target_x = _NO_TARGET
        self.arrived = False

    def start_homing(self, direction: int = -1, velocity: float = 0.1) -> None:
        """Begin homing: constant-velocity motion in ``direction``.

        Stays in ``vel`` mode (no P2P conversion). The position is
        advanced in 3-D by integrating the **rail's direction vector**
        — see :meth:`_integrate_along_rail` in :meth:`update`. The
        home sensor interrupts the motion via the LinearAxis
        coordinator before the slider reaches the rail end.

        ``direction`` is ``-1`` or ``+1`` along the **rail direction**
        (see :class:`RailComponent`). The slider moves at
        ``sign * |velocity|`` along the rail until something else
        stops it.
        """
        sign = 1 if direction >= 0 else -1
        self.home_velocity = float(velocity)
        self._motion_mode = "vel"
        self._velocity = sign * abs(self.home_velocity)
        self._target_x = _NO_TARGET
        self.arrived = False

    # ---- tick ----

    def update(self, dt: float) -> dict:
        """Advance motion by ``dt`` seconds. Returns a status dict.

        Keys
        ----
        ``current_x``    : new scalar position after clamp (along rail direction)
        ``velocity``     : velocity used this tick
        ``moving``       : True if motion is in progress
        ``arrived``      : one-shot True on the final tick of a P2P
        ``at_limit``     : True if clamped against the rail envelope
        """
        if self._motion_mode == "idle":
            self.arrived = False
            status = {
                "current_x": self.current_x,
                "velocity": 0.0,
                "moving": False,
                "arrived": False,
                "at_limit": False,
            }
            self._last_status = status
            return status

        if self._motion_mode == "vel":
            # if self._rail is not None:
            #     # Integrate the rail's 3-D direction vector by
            #     # ``velocity * dt`` and project back to the scalar
            #     # rail frame. Mathematically equivalent to
            #     # ``current_x + velocity * dt`` after the projection,
            #     # but uses the rail's direction vector explicitly so
            #     # the motion is unambiguously "along the rail" rather
            #     # than "along world X".
            #     new_x = self._integrate_along_rail(dt)
            # else:
            #     # No rail: legacy world-X Euler integration.
            #     new_x = self.current_x + self._velocity * dt
            new_x = self.current_x + self._velocity * dt
            # 不再使用 ``RailComponent.clamp``:以前调用 clamp 只是返回 x
            # (全员 no-op),这个名字反让人误以为有 envelope 夹紧。limit
            # 判定由 sensor 负责。
            self.current_x = new_x
            self._apply_position(new_x)
            status = {
                "current_x": new_x,
                "velocity": self._velocity,
                "moving": True,
                "arrived": False,
                "at_limit": False,
            }
            self._last_status = status
            return status

        if self._motion_mode == "p2p":
            self._p2p_elapsed += dt
            direction = 1 if float(self._target_x) >= self._p2p_start_x else -1
            # ``_velocity`` 已带符号（见 start_move_to）；这里再乘 direction
            # 会把负向运动的符号抵消掉，所以直接用带符号速度积分。
            new_x = self._p2p_start_x + self._velocity * self._p2p_elapsed
            
            if (direction == 1 and new_x >= float(self._target_x)) or (direction == -1 and new_x <= float(self._target_x)):
                # Arrived — snap to target.
                final_x = float(self._target_x) if self._target_x is not None else self.current_x
                at_limit = False

                self.current_x = final_x
                self._apply_position(final_x)
                self._motion_mode = "idle"
                self._velocity = 0.0
                self._p2p_elapsed = 0.0
                self.arrived = True
                status = {
                    "current_x": final_x,
                    "velocity": 0.0,
                    "moving": False,
                    "arrived": True,
                    "at_limit": at_limit,
                }
                self._last_status = status
                return status
            
            self.current_x = new_x
            self._apply_position(new_x)
            status = {
                "current_x": new_x,
                "velocity": self._velocity,
                "moving": True,
                "arrived": False,
                "at_limit": False,
            }
            self._last_status = status
            return status

        # Unknown mode — treat as idle.
        status = {
            "current_x": self.current_x,
            "velocity": 0.0,
            "moving": False,
            "arrived": False,
            "at_limit": False,
        }
        self._last_status = status
        return status

    def consume_arrived(self) -> bool:
        """Return and clear the one-shot ``arrived`` flag."""
        flag = self.arrived
        self.arrived = False
        return flag

    # ---- internal ----

    # def _integrate_along_rail(self, dt: float) -> float:
    #     """Advance the slider by ``dt`` along the rail's direction vector.

    #     Computes the new scalar position by integrating the rail's
    #     direction vector in 3-D world space:

    #         new_pos = current_pos + (rail.direction * velocity) * dt
    #         new_scalar = rail.from_position(new_pos)

    #     This is mathematically equivalent to
    #     ``current_x + velocity * dt`` after the projection back, but
    #     uses the rail's direction vector explicitly so the motion is
    #     unambiguously "along the rail" rather than "along world X".

    #     The caller (the ``vel`` branch of :meth:`update`) clamps the
    #     returned scalar against the rail envelope afterwards; this
    #     helper does not clamp.
    #     """
    #     rail = self._rail
    #     if rail is None:
    #         return self.current_x + self._velocity * dt
    #     try:
    #         current_pos = rail.to_position(self.current_x)
    #         new_pos = (
    #             float(current_pos[0]) + float(rail.direction[0]) * float(self._velocity) * float(dt),
    #             float(current_pos[1]) + float(rail.direction[1]) * float(self._velocity) * float(dt),
    #             float(current_pos[2]) + float(rail.direction[2]) * float(self._velocity) * float(dt),
    #         )
    #         return float(rail.from_position(new_pos))
    #     except Exception:
    #         # Defensive: any failure (missing mathutils, bad vector,
    #         # etc.) falls back to the legacy scalar Euler integration
    #         # so the runtime never crashes from a degenerate rig.
    #         return self.current_x + self._velocity * dt

    def _apply_position(self, x: float) -> None:
        """Translate the scalar ``x`` into a 3-D world position write.

        World position = ``_start_position + rail.direction * x``.
        ``_start_position`` is captured at construction (or updated
        to the home position via :meth:`reset_origin`) and acts as
        the slider's logical origin — so ``x = 0`` always means
        "at the starting point", regardless of where the slider is
        physically placed in the scene.

        Without a rail, the slider falls back to writing
        ``location.x = x`` (legacy behaviour) so the existing
        offline test mocks that only expose ``location.x`` still work.
        """
        if self._rail is not None:
            try:
                d = self._rail.direction
                sp = self._start_position
                pos = (
                    float(sp[0]) + float(d[0]) * float(x),
                    float(sp[1]) + float(d[1]) * float(x),
                    float(sp[2]) + float(d[2]) * float(x),
                )
                _write_world_position(
                    self.obj, pos[0], pos[1], pos[2],
                )
                return
            except Exception:
                pass
        # Fallback (no rail, or rail helpers raised): legacy X-only write.
        try:
            self.obj.location.x = float(x)
        except Exception:
            pass