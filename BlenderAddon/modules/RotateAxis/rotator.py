"""Rotator component: the body that orbits around the RotateCenter.

In a rotation rig, the **rotation pivot** is taken from the
:class:`RotateCenter` (a static reference object): the pivot is the
world-space AABB centre of the RotateCenter, and the rotation axis is
the RotateCenter's *local* axis selected via
``cfg.rotate_axis_index`` (``0`` = local X, ``1`` = local Y,
``2`` = local Z), transformed to world space through the centre's
``matrix_world``. The runtime mutates the rotator's own
``matrix_world`` each tick so that the rotator visibly orbits the
centre's pivot along that axis.

No parent relationship between Rotator and RotateCenter is required.
The runtime reads the current angle from
:meth:`RotateCenterComponent.read_angle` (kept as bookkeeping) and
writes the new angle back via
:meth:`RotateCenterComponent.apply_rotation`, then translates that
delta into a world-space rotation of the rotator around the centre's
pivot.

Limit handling
--------------
Rotation axes have no physical "rail" — the rail concept does not
generalise to angles because there's no straight-line bounding box.
Instead, motion bounds are enforced exclusively by limit sensors
(``pos_limit`` / ``neg_limit``) terminating :class:`STATE_MOVING_VEL`.
If neither sensor is wired, the runtime simply trusts the command —
the runtime's :meth:`update` does not clamp on its own. (Industrial
rotary stages rarely need a soft rail fallback: over-travel is a
hardware fault, not a normal-mode condition.)

Sensors see the TriggerShim if one exists, falling back to the Rotator
body itself; this is the same contract as the linear axis.
"""

from __future__ import annotations

import math
from typing import Optional


# Sentinel for "no P2P target in progress".
_NO_TARGET = None


def _local_axis_vector(axis_index: int):
    """Return a unit vector for the requested local axis ``0/1/2``.

    Imports ``mathutils.Vector`` lazily so the module loads in offline
    tests that don't have Blender. Returns a plain 3-tuple when
    ``mathutils`` is unavailable — callers handle either shape.
    """
    if axis_index == 0:
        axis = (1.0, 0.0, 0.0)
    elif axis_index == 1:
        axis = (0.0, 1.0, 0.0)
    else:
        axis = (0.0, 0.0, 1.0)
    try:
        from mathutils import Vector  # type: ignore
        return Vector(axis)
    except Exception:
        return axis


class RotateRotatorComponent:
    """Wraps a Rotator mesh + its RotateCenter as one motion unit.

    The two are **not** required to be parented: the runtime rotates
    the rotator around the centre's world pivot each tick, so the rig
    is free to use whatever scene graph the artist prefers.

    The RotateCenter is a **static** reference object — its
    ``rotation_euler`` is never touched by the runtime. The rotator
    tracks its own angle internally and translates each delta into a
    world-matrix rotation around the centre's pivot.
    """

    __slots__ = (
        "obj",
        "center",
        "default_velocity",
        "home_velocity",
        "_motion_mode",
        "_velocity",
        "_target_angle",
        "_p2p_start_angle",
        "_p2p_duration",
        "_p2p_elapsed",
        "_current_angle",
        "arrived",
    )

    def __init__(self, obj, *, center=None,
                 default_velocity: float = 0.5,
                 home_velocity: float = 0.1):
        self.obj = obj
        self.center = center
        self.default_velocity = float(default_velocity)
        self.home_velocity = float(home_velocity)
        # Initialise from the centre's current angle so the runtime
        # never starts at "0°" when the artist has pre-rotated the rig.
        # Subsequent angle bookkeeping lives entirely on this rotator —
        # the centre is left untouched so it stays a static pivot.
        self._motion_mode = "idle"
        self._velocity: float = 0.0
        self._target_angle: Optional[float] = _NO_TARGET
        self._current_angle: float = float(self.center.read_angle()) if self.center is not None else 0.0
        self._p2p_start_angle: float = float(self._current_angle)
        self._p2p_duration: float = 0.0
        self._p2p_elapsed: float = 0.0
        self.arrived: bool = False

    # ---- read-only views ----

    @property
    def mode(self) -> str:
        return self._motion_mode

    @property
    def velocity(self) -> float:
        return self._velocity

    @property
    def target_angle(self):
        return self._target_angle

    @property
    def current_angle(self) -> float:
        """Return the current rotation angle in degrees.

        Authoritative angle state for the rotator — kept internally
        rather than read from the centre's Euler (which is static).
        """
        return float(self._current_angle)

    @property
    def at_limit(self) -> bool:
        """Always False — rotation axes have no soft rail clamp.

        Present so the runtime's state-transition code can mirror the
        linear axis without a branch on module kind.
        """
        return False

    # ---- direct set ----

    def set_angle(self, angle_deg: float) -> None:
        """Force the rotation to a specific angle (degrees).

        Used by homing (when the home sensor triggers) and by external
        callers. ``angle_deg`` is interpreted as the raw value to
        write to the rotator's bookkeeping; it is NOT normalised.
        Applies the corresponding visual rotation around the centre's
        pivot immediately.
        """
        target = float(angle_deg)
        delta = target - float(self._current_angle)
        self._current_angle = target
        self._p2p_start_angle = target
        self._apply_world_rotation(delta)

    # ---- motion commands ----

    def start_idle(self) -> None:
        """Cancel any in-flight motion."""
        self._motion_mode = "idle"
        self._velocity = 0.0
        self._target_angle = _NO_TARGET
        self.arrived = False

    def start_move_to(self, target_angle: float, velocity: float) -> None:
        """Begin point-to-point rotation toward ``target_angle``.

        LinearAxis 同款语义：**速度 × dt 步进**，不再按固定时长/插值。
        ``velocity`` 为角速度绝对值（deg/s，通常来自 cfg.angular_speed）；
        运动方向由目标相对当前的连续位移符号决定（不回绕）。

        每 tick 由 :meth:`update` 前进一步并做越界检测：一旦本步越过
        ``target`` 就吸附到 target、置 ``arrived`` 并停下。
        """
        start = float(self._current_angle)
        target = float(target_angle)
        delta = target - start
        speed = abs(float(velocity))
        if abs(delta) < 1e-9 or speed < 1e-9:
            # 已到目标 / 速度为零：不产生位移，由下一 tick 判定为到达。
            self._motion_mode = "p2p"
            self._velocity = 0.0
            self._target_angle = target
            self._p2p_start_angle = start
            self.arrived = False
            return
        sign = 1.0 if delta > 0.0 else -1.0
        self._motion_mode = "p2p"
        self._p2p_start_angle = start
        self._target_angle = target
        self._velocity = sign * speed
        self.arrived = False

    def start_velocity(self, omega_deg_per_s: float) -> None:
        """Begin constant-angular-velocity motion at ``omega_deg_per_s``."""
        self._motion_mode = "vel"
        self._velocity = float(omega_deg_per_s)
        self._target_angle = _NO_TARGET
        self.arrived = False

    def start_homing(self, direction: int = -1, velocity: float = 0.1) -> None:
        """Begin homing: constant angular velocity toward ``direction``.

        ``direction`` is ``-1`` (default — clockwise in our atan2
        convention) or ``+1`` (counter-clockwise). Magnitude comes
        from ``velocity``. The home sensor terminates the motion; the
        rotator keeps integrating until the runtime calls
        :meth:`start_idle` or :meth:`set_angle`.
        """
        sign = 1 if direction >= 0 else -1
        self.home_velocity = float(velocity)
        self._motion_mode = "vel"
        self._velocity = sign * abs(self.home_velocity)
        self._target_angle = _NO_TARGET
        self.arrived = False

    # ---- tick ----

    def update(self, dt: float) -> dict:
        """Advance motion by ``dt`` seconds. Returns a status dict.

        Keys
        ----
        ``current_angle`` : rotation angle after integration (degrees)
        ``velocity``      : angular velocity used this tick (deg/s)
        ``moving``        : True if motion is in progress
        ``arrived``       : one-shot True on the final tick of a P2P
        ``at_limit``      : always False (no soft rail on rotate)
        """
        if self._motion_mode == "idle":
            self.arrived = False
            status = {
                "current_angle": float(self._current_angle),
                "velocity": 0.0,
                "moving": False,
                "arrived": False,
                "at_limit": False,
            }
            return status

        if self._motion_mode == "vel":
            current = float(self._current_angle)
            new_angle = current + self._velocity * dt
            self._current_angle = new_angle
            self._apply_world_rotation(new_angle - current)
            status = {
                "current_angle": new_angle,
                "velocity": self._velocity,
                "moving": True,
                "arrived": False,
                "at_limit": False,
            }
            return status

        if self._motion_mode == "p2p":
            current = float(self._current_angle)
            target = float(self._target_angle) if self._target_angle is not None else current
            step = self._velocity * dt
            rem = target - current
            # 速度×dt 步进；本步越过 target（或已到达）→ 吸附到位并结束。
            if abs(rem) < 1e-9 or abs(step) < 1e-9:
                # 已到达 target，或速度≈0（原地结束）：停在当前位置，
                # 不产生向 target 的跳跃。
                final = current if abs(rem) >= 1e-9 else target
                delta = final - current
                self._current_angle = final
                self._p2p_start_angle = final
                if abs(delta) > 0.0:
                    self._apply_world_rotation(delta)
                self._motion_mode = "idle"
                self._velocity = 0.0
                self.arrived = True
                status = {
                    "current_angle": final,
                    "velocity": 0.0,
                    "moving": False,
                    "arrived": True,
                    "at_limit": False,
                }
                return status
            nxt = current + step
            if (target - nxt) * rem <= 0.0:
                # 这一步跨过了 target：直接吸附到 target。
                self._current_angle = target
                self._apply_world_rotation(target - current)
                self._p2p_start_angle = target
                self._motion_mode = "idle"
                self._velocity = 0.0
                self.arrived = True
                status = {
                    "current_angle": target,
                    "velocity": 0.0,
                    "moving": False,
                    "arrived": True,
                    "at_limit": False,
                }
                return status
            self._current_angle = nxt
            self._apply_world_rotation(nxt - current)
            status = {
                "current_angle": nxt,
                "velocity": self._velocity,
                "moving": True,
                "arrived": False,
                "at_limit": False,
            }
            return status

        # Unknown mode — treat as idle.
        status = {
            "current_angle": float(self._current_angle),
            "velocity": 0.0,
            "moving": False,
            "arrived": False,
            "at_limit": False,
        }
        return status

    def consume_arrived(self) -> bool:
        """Return and clear the one-shot ``arrived`` flag."""
        flag = self.arrived
        self.arrived = False
        return flag

    # ---- world-space visual rotation ----

    def _apply_world_rotation(self, delta_deg: float) -> None:
        """Rotate the rotator by ``delta_deg`` around the centre's pivot.

        The rotation axis is the RotateCenter's local axis
        (``cfg.rotate_axis_index``) transformed to world space through
        the centre's ``matrix_world``. The pivot is the rotate
        centre's world AABB centre.

        This is the visible half of the rotation pipeline: the angle
        bookkeeping on the RotateCenter already happened in
        :meth:`update`; here we just translate that bookkeeping into a
        matrix mutation on the rotator.

        实现细节
        --------
        旧实现直接写 ``obj.matrix_world``。当 rotator 有 parent
        (例:挂在 LinearAxis 的 slider 上,或 RotateAxis 自身在某动轴
        下),Blender 写完 world 后会立刻反推 ``matrix_local = parent.
        matrix_world.inverted() @ world`` 并把 matrix_local 分解回
        ``location / rotation_euler / scale``;euler 分解每 tick 都
        引入舍入,多 tick 累积后 rotator 视觉上"漂离" 旋转中心
        ("rotate 跑不见了")。

        新实现把 ``pivot_world`` 和 ``world_axis`` 用 ``parent.
        matrix_world.inverted()`` 转 obj 的 local 空间, 在 local 空间
        做 rotate-around-pivot, 直接写 ``obj.matrix_local``。Blender
        不再反推, 没有累积漂移。无 parent 时 ``parent_inv`` = 单位
        阵, 行为与原 world 写法等价。

        Guards
        ------
        Silently no-ops on any failure (missing centre, missing
        ``mathutils``, mock objects in offline tests). Callers must
        not depend on this method raising — the angle bookkeeping is
        authoritative and survives even if the visual write fails.
        """
        if abs(float(delta_deg)) < 1e-12:
            return
        if self.center is None:
            return
        try:
            from mathutils import Matrix  # type: ignore
        except Exception:
            return  # Offline path — angle bookkeeping already done.

        # 每 tick 刷新缓存的 pivot。``pivot_world`` 是带缓存的属性
        # (只懒初始化一次), 而 center 的世界位置会随宿主(LinearAxis /
        # slider 等) 移动而变。不刷新的话, rotator 会绕“过期的 pivot”
        # 旋转 —— 例:LinearAxis home 带动 host 移位后, RotateAxis 再
        # home 时 rotator 会飞离当前 rotate center(用户看到的
        # “rotate 部分跑不见了”)。refresh_pivot 自己很便宜
        # (纯函数, 只读 matrix_world + bound_box), 可以每 tick 调。
        try:
            self.center.refresh_pivot()
        except Exception:
            pass
        try:
            pivot = self.center.pivot_world
        except Exception:
            return
        if pivot is None:
            return

        local_axis = _local_axis_vector(self.center.axis_index)
        try:
            m = self.center.obj.matrix_world
            # The upper-left 3x3 of ``matrix_world`` is the rotation
            # (and scale, but the rig treats scale as unit). Transforming
            # the local axis through it yields the world-space axis.
            world_axis = (m.to_3x3() @ local_axis)
            # ``Matrix.Rotation`` expects a normalised axis vector;
            # a zero-length vector (degenerate rig) means we can't
            # visualise the motion — bail out silently.
            # NB: Blender's ``mathutils.Vector.normalize()`` mutates
            # in place and returns ``None``; use ``.normalized()``
            # (returns a new vector) instead so the assignment below
            # doesn't silently null out ``world_axis``.
            if hasattr(world_axis, "length") and world_axis.length < 1e-12:
                return
            if hasattr(world_axis, "normalized"):
                world_axis = world_axis.normalized()
            rot_deg = math.radians(float(delta_deg))
            obj = self.obj

            # 把 pivot / axis 从 world 空间转到 obj 的 local 空间
            # (有 parent 时;无 parent 时 parent_inv = I,结果与原写法等价)
            if obj.parent is not None:
                try:
                    parent_inv = obj.parent.matrix_world.inverted()
                except Exception:
                    parent_inv = Matrix.Identity(4)
            else:
                parent_inv = Matrix.Identity(4)
            try:
                pivot_local = parent_inv @ pivot
                axis_local = parent_inv.to_3x3() @ world_axis
                if hasattr(axis_local, "length") and axis_local.length < 1e-12:
                    return
                if hasattr(axis_local, "normalized"):
                    axis_local = axis_local.normalized()
            except Exception:
                return

            rot = Matrix.Rotation(rot_deg, 4, axis_local)
            to_pivot = Matrix.Translation(-pivot_local)
            from_pivot = Matrix.Translation(pivot_local)
            # 写 local 而非 world:避免 Blender 反推 matrix_world ->
            # matrix_local 时 euler 分解舍入累积, 见 docstring。
            obj.matrix_local = (
                from_pivot @ rot @ to_pivot @ obj.matrix_local
            )
        except Exception:
            # Mock objects in offline tests don't expose the full
            # matrix API; in real Blender this branch is unreachable.
            return


# Backward-compatible alias for older imports.
RotateSliderComponent = RotateRotatorComponent