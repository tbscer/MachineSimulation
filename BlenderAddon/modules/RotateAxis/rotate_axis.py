"""RotateAxis: passive rotational axis runtime owned by :class:`SimulationManager`.

The V0_3 analogue of :mod:`LinearAxis.axis`. It is deliberately
**passive**: it owns no timers, no threads, and no discovery. The
owning :class:`SimulationManager` calls :meth:`update` once per
simulation tick.

Each rotation axis bundles:
- one :class:`RotateCenterComponent` (the static symmetry centre —
  the rotation pivot; defines the world-space pivot point and the
  rotation axis)
- one :class:`RotateRotatorComponent` (the orbiting body; rotated
  directly around the centre's pivot by the local axis selected via
  ``rotate_axis_index``. No parent-child relationship is required.)
- N :class:`SensorComponent` (point-mode overlap detectors — the
  trigger shim's centre must reach the sensor body)
- zero or one :class:`TriggerShimComponent` (clean AABB source)

Public API
----------
Commands (callable directly, or via the ``axis_cmd_*`` custom property
bridge that :meth:`_consume_cmd` reads on every tick):

- :meth:`move_to(target_angle, duration_s)` — point-to-point rotation
- :meth:`set_velocity(omega)`               — constant angular velocity
- :meth:`home(direction)`                   — homing toward ``-Z`` (default)
- :meth:`stop`                              — immediate cancel
- :meth:`idle`                              — alias of :meth:`stop`
- ``reset_collision``                       — clear the scene-level
  ``motion_simulation_collision`` flag and unblock this axis. Only
  command honoured while the axis is in ``STATE_BLOCKED``.

State machine
-------------
``IDLE``            — not moving
``HOMING``          — constant angular velocity toward the home sensor
``MOVING_P2P``      — interpolating toward a target angle
``MOVING_VEL``      — integrating a constant angular velocity
``STOPPED_AT_LIMIT` — terminal: hit a ``pos_limit`` / ``neg_limit``
                      sensor while in ``vel`` mode
``BLOCKED``         — terminal: rotator contacted a non-axis obstacle.
                      Axis refuses every command except ``reset_collision``.

Limit sensors (``pos_limit`` / ``neg_limit``) terminate
``MOVING_VEL`` early with ``stop_reason="limit"``. There is no soft
"rail" clamp for rotational axes; over-travel is treated as a
hardware fault, not a normal-mode condition.

The rotation centre is the source of truth for the current angle; the
runtime reads it via :meth:`RotateCenterComponent.read_angle` and
writes via :meth:`RotateCenterComponent.apply_rotation`.

Collision detection
-------------------
Mirrors :class:`LinearAxis`: on every tick the runtime consults the
shared :class:`CollisionEngine` to see whether the rotator's mesh
overlaps any obstacle. A hit drives the axis into ``STATE_BLOCKED``
with ``stop_reason="collision"`` and writes
``scene["motion_simulation_collision"] = True``. The flag is the
single source of truth — every axis that sees it on the next tick
becomes blocked. A ``reset_collision`` command releases all blocked
axes.
"""

from __future__ import annotations

import json
from typing import List, Optional

try:
    from ...framework import BaseSimulationModule, SimulationCommand, is_object_alive
    from .rotator import RotateRotatorComponent
    from .rotate_center import RotateCenterComponent
    from ..components.collision import STOP_REASON_COLLISION, module_self_check
    from ..components.sensor.base_sensor import aggregate_axis_sensor_states
except ImportError:  # pragma: no cover - offline / direct-script import path
    from framework import BaseSimulationModule, SimulationCommand, is_object_alive  # noqa: F401
    from modules.RotateAxis.rotator import RotateRotatorComponent  # noqa: F401
    from modules.RotateAxis.rotate_center import RotateCenterComponent  # noqa: F401
    from modules.components.collision import (  # noqa: F401
        STOP_REASON_COLLISION,
        module_self_check,
    )
    from modules.components.sensor.base_sensor import aggregate_axis_sensor_states  # noqa: F401


# State constants — names mirror :mod:`LinearAxis.axis` so any cross-
# module state reader sees a familiar vocabulary.
STATE_IDLE = "idle"
STATE_HOMING = "homing"
STATE_MOVING_P2P = "moving_p2p"
STATE_MOVING_VEL = "moving_vel"
STATE_STOPPED_AT_LIMIT = "stopped_at_limit"
STATE_BLOCKED = "blocked"

ALL_STATES = (
    STATE_IDLE,
    STATE_HOMING,
    STATE_MOVING_P2P,
    STATE_MOVING_VEL,
    STATE_STOPPED_AT_LIMIT,
    STATE_BLOCKED,
)

# ``reset_collision`` **不是**模块的 action —— 它是 collision engine 的
# 系统级操作（清 scene-level collision marker 并释放所有 BLOCKED 模块）。
# 严禁作为 ``axis_cmd_*`` 命令或 ``apply_command`` action 送达；交由
# :meth:`CollisionEngine.clear_collision` / ``set_disabled`` 完成。
VALID_ACTIONS = ("idle", "home", "jog", "move_to", "velocity", "stop")


class RotateAxisRuntime(BaseSimulationModule):
    """Passive runtime for one rotational axis, exposed as a simulation module."""

    __slots__ = (
        "axis_id",
        "host_obj",
        "center",
        "rotator",
        "slider",
        "shim",
        "sensors",
        "state",
        "home_done",
        "home_angle",
        "zero_offset",
        "_home_command_direction",
        "_home_command_velocity",
        "_home_origin_passed",
        # When entering home() while the home sensor is already on,
        # this axis first drives the rotator OPPOSITE to the requested
        # direction until the sensor releases, then resumes homing.
        # See :meth:`home` + the HOMING branch of
        # :meth:`_check_transitions`.
        "_home_backoff",
        "_last_seq",
        "_stop_reason",
        "_collision_engine",
        "category",
    )

    def __init__(self, host_obj, *, center, rotator, sensors,
                 shim=None, axis_id: Optional[str] = None,
                 collision_engine=None):
        super().__init__(module_id=axis_id if axis_id is not None else host_obj.name,
                         kind="rotate_axis",
                         category="axes")
        self.host_obj = host_obj
        self.axis_id = self.module_id
        # Allow None for offline / fallback construction (test harness,
        # ``build_module`` without a configured rig). The runtime guards
        # against missing components in :meth:`update` so this is safe.
        self.center = center
        self.rotator = rotator
        self.slider = self.rotator
        self.shim = shim
        self.sensors: List = list(sensors) if sensors is not None else []
        # Runtime state.
        self.state: str = STATE_IDLE
        self.home_done: bool = False
        self.home_angle: Optional[float] = None
        self.zero_offset: float = 0.0
        self._home_command_direction: int = -1
        self._home_command_velocity: float = 0.1
        self._home_origin_passed: bool = False
        # Initialise to 0 so the default-empty ``axis_cmd_seq`` on a
        # fresh slider is treated as "no command pending" rather than
        # a brand-new seq-0 command that the runtime would otherwise
        # consume (and idle) on the very first tick.
        self._last_seq: int = int(self.rotator.obj.get("axis_cmd_seq", 0))
        self._stop_reason: str = ""
        # Shared :class:`CollisionEngine`. ``None`` is acceptable for
        # offline tests; collision checks are skipped silently.
        self._collision_engine = collision_engine
        # Push initial state to custom properties so the first external
        # read sees a well-formed schema even before the first tick.
        # Guarded because :attr:`rotator` may be None for offline
        # construction.
        try:
            self._write_state_props()
        except Exception:
            pass

    # ---- public: commands (Python API) ----

    def move_to(self, target_angle: float, velocity: float = 1.0) -> None:
        """Begin point-to-point rotation. Target is in degrees.

        The ``target_angle`` parameter is interpreted relative to the
        homed origin if the axis has been homed; otherwise it is
        treated as world-space (the centre's raw rotation Euler).

        ``velocity`` 是角速度（deg/s）。``target_angle`` 按 **连续角度** 解释：
        它是相对 homed 原点展开的显示角，例如显示角 -50 时请求 -250 会继续
        向负方向转 200°，不做“最短弧掉头”。需要“按速度一直转、直到撞限位”
        请走 ``jog``（VEL 路径）。
        """
        if self.state == STATE_BLOCKED:
            return  # Refuse new commands until reset_collision.
        # ``target_angle`` 是相对 homed 原点的 **连续显示角**；换算到世界系
        # 只需加上 ``zero_offset``，**不能**再做 (-180, 180] 归一化 ——
        # 归一化会把 ``zero_offset`` 里的整圈信息抹掉。
        # 反例（实测）：zero_offset≈356.667 时 ``move_to(30)`` 会被折成
        # world 26.667（= 显示角 -330°），恰好等于当前角，于是“原地不动”。
        world_target = float(target_angle) + self.zero_offset
        self.rotator.start_move_to(world_target, float(velocity))
        self.state = STATE_MOVING_P2P
        self._stop_reason = ""

    def set_velocity(self, omega: float) -> None:
        """Begin constant angular velocity motion at ``omega`` deg/sec."""
        if self.state == STATE_BLOCKED:
            return
        self.rotator.start_velocity(float(omega))
        self.state = STATE_MOVING_VEL
        self._stop_reason = ""

    def home(self, direction: int = -1, velocity: float = 0.1) -> None:
        """Begin homing toward ``direction`` (``-1`` default).

        Back-off step
        ---------------
        If the home sensor is already triggered when ``home()`` is
        invoked, the axis does **not** blindly accept the read. It
        first drives the rotator in the OPPOSITE direction until the
        sensor releases, then starts the requested homing in
        ``direction``. Idempotent regardless of where the rotator
        currently sits.

        ``home_done`` 翻转契约
        -----------------------
        接到 ``home`` 命令的当下 ``home_done`` 必须立刻翻成 ``False``,
        直到 :meth:`_check_transitions` 在 :attr:`STATE_HOMING` 期间
        真正撞到 home sensor 才回 ``True``。所有 4 条命令路径 —— UI
        面板按钮、``apply_command("home")`` (RPC)、MCP / 外部直接写
        ``axis_cmd_*`` props 由 :meth:`_consume_cmd` 消费、以及
        Python 直接调 :meth:`home` —— 最终都汇合到本方法,因此本入口
        是翻转 ``home_done`` 的唯一收口点,无需在每条入口重复。
        外部观察者(state_push / MCP / Dev panel)由此可以清楚区分
        "homing 进行中" 与 "从未 home 或已完成"。

        BLOCKED 期间 :meth:`_consume_cmd_blocked` 与 ``apply_command``
        的 BLOCKED 守卫会静默丢弃 home 命令,根本走不到本方法后面的
        逻辑,``home_done`` 因此保持不变 —— 与状态机"拒绝执行"的语义
        一致。
        """
        if self.state == STATE_BLOCKED:
            return
        # 命令已被状态机正式接收:先把 ``home_done`` 清零,后续在
        # ``_check_transitions`` 中真正撞到 home sensor 时再回 True。
        # 收口在 ``home()`` 入口一处,可同时覆盖 UI / RPC / MCP /
        # Python 直调四条路径。
        self.home_done = False
        self._home_command_direction = int(direction)
        self._home_command_velocity = float(velocity)
        self._home_origin_passed = False
        if self._is_home_sensor_triggered():
            self._home_backoff = True
            self.rotator.start_homing(
                direction=-self._home_command_direction,
                velocity=self._home_command_velocity,
            )
        else:
            self._home_backoff = False
            self.rotator.start_homing(
                direction=self._home_command_direction,
                velocity=self._home_command_velocity,
            )
        self.state = STATE_HOMING
        self._stop_reason = ""

    def _is_home_sensor_triggered(self) -> bool:
        """True iff any sensor tagged ``is_home`` is currently on.

        Used by :meth:`home` to decide whether to enter the back-off
        sub-state on entry. Ignored / non-home sensor kinds return
        False. Multiple home sensors are OR-ed (any one on counts).
        """
        for s in self.sensors:
            if getattr(s, "is_home", False) and s.is_triggered:
                return True
        return False

    def stop(self) -> None:
        """Cancel motion. ``stop_reason`` reflects who called."""
        self.rotator.start_idle()
        self.state = STATE_IDLE
        if not self._stop_reason:
            self._stop_reason = "cmd_stop"

    def idle(self) -> None:
        """Alias of :meth:`stop`. ``stop_reason`` is ``"cmd_idle"``."""
        self.rotator.start_idle()
        self.state = STATE_IDLE
        if not self._stop_reason:
            self._stop_reason = "cmd_idle"

    def reset_collision(self) -> None:
        """Clear the scene-level collision flag and release this axis.

        Idempotent. Mirrors :meth:`LinearAxis.reset_collision`.
        """
        if self._collision_engine is not None:
            self._collision_engine.clear_collision()
        if self.state == STATE_BLOCKED and self.rotator is not None:
            self.rotator.start_idle()
            self.state = STATE_IDLE
            self._stop_reason = ""

    # ---- public: module API ----

    def apply_command(self, cmd: SimulationCommand) -> None:
        """Dispatch a generic command to the rotation runtime."""
        action = getattr(cmd, "action", "")
        payload = getattr(cmd, "payload", {}) or {}
        # BLOCKED 期间拒绝所有 motion 命令。``reset_collision`` 不是
        # 模块的 action(它在 ``VALID_ACTIONS`` 之外),交由
        # :meth:`CollisionEngine.clear_collision` / ``set_disabled`` 完成。
        # 原本是各分支内部的 ``if self.state == STATE_BLOCKED: return``
        # 静默抹掉调用意图；现在统一在入口处抛出
        # :class:`AxisBlockedError`,由 RPC / UI 调用方识别后报红或返
        # 专门的错误码,并调 ``engine.clear_collision()`` 解锁。
        # 懒导入避免 rotate_axis 与 axis_errors 的循环依赖。
        if self.state == STATE_BLOCKED:
            try:
                from ..axis_errors import AxisBlockedError
            except ImportError:  # pragma: no cover - 离线 fallback
                AxisBlockedError = RuntimeError
            raise AxisBlockedError(
                f"RotateAxis {self.axis_id!r} is blocked by collision; "
                f"call CollisionEngine.clear_collision() to clear the block."
            )
        if action == "move_to":
            # 角速度优先取 ``velocity``（deg/s，与 LinearAxis 字段名一致）；
            # 只有旧调用方显式只给了 ``duration_s`` 时才退回它。
            vel = payload.get("velocity", None)
            if vel is None:
                vel = payload.get("duration_s", 1.0)
            self.move_to(float(payload.get("target_angle", 0.0)), float(vel))
        elif action == "velocity":
            self.set_velocity(float(payload.get("velocity", 0.0)))
        elif action == "jog":
            # Jog：按显式方向 + 角速度幅度定速旋转，直到撞 limit sensor
            # 或被 stop。
            #
            # 走 ``set_velocity`` 的 MOVING_VEL 路径（极性正确：
            # neg_limit 挡负向、pos_limit 挡正向）。不能用 ``move_to``：
            # 它会把 target 归一化到 (-180, 180]，超大 target 会被折叠成
            # 一个小角度，导致 jog 只能转到 ~80° 就“到点”停下。
            direction = int(payload.get("direction", 1))
            velocity = abs(float(payload.get("velocity", 0.0)))
            self.set_velocity(direction * velocity)
        elif action == "home":
            # 与 LinearAxis 同构: UI 路径走 ``axis_ops.send_command`` 写
            # axis_cmd_* props + 增 axis_cmd_seq, 下一 tick 由 _consume_cmd
            # 统一调 self.home(). RPC 路径走同一份代码, 保证:
            # * direction / velocity 缺省取自 host 端 cfg(host.rotate_axis.
            #   home_direction / angular_home_speed), 与 UI 面板一致, 不再
            #   硬编码 -1 / 0.1;
            # * BLOCKED 期间静默丢弃(同 _consume_cmd_blocked 守门语义,
            #   UI 那边是发 ERROR 报告, RPC 静默返 ok=true);
            # * 反转逻辑 (_check_transitions) 必然在 _consume_cmd 之后
            #   跑, 与 UI 完全同构 —— 这是修“撞 limit 不换向”bug 的关键:
            #   旧路径可能因 _home_command_velocity 默认为 0.1 而用户
            #   cfg.angular_home_speed 是别的值, 造成与 UI 不一致, 反转
            #   走“反转 后” 与 UI 期望不符, 看似“不换向”。
            if self.state == STATE_BLOCKED:
                return
            cfg = getattr(self.host_obj, "rotate_axis", None)
            cfg_direction = getattr(cfg, "home_direction", -1) if cfg is not None else -1
            cfg_speed = getattr(cfg, "angular_home_speed", 10.0) if cfg is not None else 10.0
            direction = int(payload.get("direction", cfg_direction))
            velocity = float(payload.get("velocity", cfg_speed))
            # 懒导入避免 rotate_axis.py <-> axis_ops.py 循环 import
            # (axis_ops 顶层 from .rotate_axis import VALID_ACTIONS)。
            from .axis_ops import send_command
            send_command(
                self.axis_id, action="home",
                home_direction=direction, velocity=velocity,
            )
            # 不直接调 self.home() —— 与 UI 同构, 等 _consume_cmd 下一 tick
            # 调。
        elif action in {"stop", "idle"}:
            getattr(self, action)()

    def reset(self) -> None:
        """Reset the runtime to the idle state."""
        self.stop()
        self.home_done = False
        self.home_angle = None
        self.zero_offset = 0.0
        self._home_origin_passed = False
        self._home_backoff = False
        self._stop_reason = ""

    def snapshot(self) -> dict:
        """Return a compact state snapshot for the manager.

        Sensor 状态以 axis-agnostic 的三布尔字段暴露：
        `home_sensor` / `pos_limit_sensor` / `neg_limit_sensor`。
        RotateAxis 内部 kind 命名（`home` / `pos_limit` / `neg_limit`）
        与对外字段一致，无需额外映射。多个同类型 sensor 按 OR 聚合。
        """
        sensors = aggregate_axis_sensor_states(self.sensors)
        return {
            "module_id": self.module_id,
            "kind": self.kind,
            "name": self.axis_id,
            "state": self.state,
            "home_done": self.home_done,
            "current_angle": float(self.rotator.current_angle - self.zero_offset),
            "velocity": float(self.rotator.velocity),
            "stop_reason": self._stop_reason,
            "home_sensor": bool(sensors["home"]),
            "pos_limit_sensor": bool(sensors["pos_limit"]),
            "neg_limit_sensor": bool(sensors["neg_limit"]),
        }

    # ---- public: collision structure declaration ----

    def collision_structure(self):
        """声明 RotateAxis 的碰撞结构(见 :class:`CollisionStructure`)。

        - **members**:host + center + rotator + shim。是真正会参与碰撞
          BVH 的对象 —— engine 会把它们的 BVH 打成一个 per-axis group,
          跨轴 rotator 撞到这组才算碰撞。组内任意两个对象**永不**做碰
          撞检测。子树展开与其它模块 host 剪枝由 engine 统一处理。
        - **bodies**:rotator —— 唯一的运动体。
        - **sensors**:全部 sensor(``home`` / ``pos_limit`` /
          ``neg_limit``)。Sensor mesh **不**打进 per-axis BVH(它是
          触发器件,不是 obstacle),但名字会被记入
          ``ResolvedStructure.sensor_names``,供 dev 面板 / 跨轴 sensor
          排除使用。Sensor 的触发判定依然走
          ``RotateAxis._update_sensors`` 这条独立路径,与 collision
          engine 完全无关。

        历史背景：与 :meth:`LinearAxis.collision_structure` 同款修复，
        把 sensor 从 members 移到 sensors。早期版本让传感器 mesh 进
        per-axis BVH,跨轴 rotator 经过对方 axis 的 sensor 物理位置会
        被 engine 误判为撞了对方 group。
        """
        try:
            from ..components.collision_structure import (
                CollisionStructure,
                unwrap_object,
            )
        except ImportError:  # pragma: no cover - 离线/直接脚本 import 路径
            from modules.components.collision_structure import (  # type: ignore
                CollisionStructure,
                unwrap_object,
            )

        members: List = [self.host_obj]
        for field in ("center", "rotator", "shim"):
            members.append(unwrap_object(getattr(self, field, None)))
        body = unwrap_object(getattr(self.rotator, "obj", None))
        sensors: List = [
            unwrap_object(getattr(sensor, "obj", sensor))
            for sensor in (self.sensors or ())
        ]
        return CollisionStructure(
            module_id=self.module_id,
            host=self.host_obj,
            members=tuple(m for m in members if m is not None),
            bodies=(body,) if body is not None else (),
            sensors=tuple(s for s in sensors if s is not None),
        )

    # ---- public: tick ----

    def update(self, dt: float) -> None:
        """Run one simulation tick. Called by :class:`SimulationManager`.

        Order:
        1. Validate that every referenced Blender Object still exists
           (stale references are auto-removed by the manager).
        2. Read the scene-level collision marker. If set, force the
           axis into ``STATE_BLOCKED`` and accept only ``reset_collision``.
        3. Consume any new command written into the Slider's
           ``axis_cmd_*`` custom properties (seq strictly increasing is
           the trigger).
        4. Refresh sensor overlaps.
        5. Advance rotator motion.
        6. Run BVH-based collision check on the rotator's new position;
           on hit, drive the axis into ``STATE_BLOCKED`` and set the
           scene-level marker.
        7. Apply remaining state transitions (homing → home sensor,
           p2p → arrived, vel → limit sensor).
        8. Mirror state back to ``axis_*`` custom properties for
           read-side MCP access.

        Components may be ``None`` for offline / fallback construction;
        in that case every step is a no-op.
        """
        if self.rotator is None:
            return
        if not self._references_alive():
            self._alive = False
            return

        # Auto-unblock on marker clear — see LinearAxis.update for the
        # full rationale. The flag was cleared by some axis's
        # ``reset_collision``; this axis does not need its own command.
        if (self._collision_engine is not None
                and not self._collision_engine.read_marker()
                and self.state == STATE_BLOCKED):
            self._clear_blocked_state_only()

        # Scene-level collision gating. See LinearAxis.update for the
        # rationale — the scene marker is the single source of truth so
        # every blocked axis sees the flag on the next tick.
        if self._collision_engine is not None and self._collision_engine.read_marker():
            if self.state != STATE_BLOCKED:
                self._enter_blocked_state()
            self._consume_cmd_blocked()
            try:
                self._write_state_props()
            except Exception:
                pass
            return

        self._consume_cmd()
        self._update_sensors()
        self._update_rotator(dt)

        # 碰撞:生产路径由 ``CollisionEngine.step`` 在每个 tick 的模块
        # 循环之后统一计算并回调 ``on_collision_hit``;下面这段只是
        # “引擎未接管模块名册”(离线测试替身 / 外部脚本)时的过渡兼容,
        # 行为与重构前一致。
        if self._collision_engine is not None:
            try:
                import bpy
                depsgraph = bpy.context.evaluated_depsgraph_get()
            except Exception:
                depsgraph = None
            body_obj = getattr(self.rotator, "obj", None)
            if module_self_check(self._collision_engine, self, body_obj,
                                 depsgraph):
                self._enter_blocked_state()
                try:
                    self._write_state_props()
                except Exception:
                    pass
                return

        self._check_transitions()
        try:
            self._write_state_props()
        except Exception:
            pass

    def _references_alive(self) -> bool:
        """Return True iff every referenced Blender Object still exists.

        If the user deletes the rig, the scene reloads with different
        objects, or any referenced Object is removed in the viewport,
        the runtime would otherwise raise
        ``ReferenceError: StructRNA of type Object has been removed``
        on every tick. Detecting the stale reference here lets us
        flag the module as broken so the manager can unregister it.
        """
        if not is_object_alive(self.host_obj):
            return False
        if self.rotator is None or not is_object_alive(self.rotator.obj):
            return False
        if self.center is not None and not is_object_alive(self.center.obj):
            return False
        if self.shim is not None and not is_object_alive(self.shim.obj):
            return False
        for s in self.sensors:
            if not is_object_alive(getattr(s, "obj", None)):
                return False
        return True

    # ---- cmd ----

    def _consume_cmd(self) -> None:
        """Pull a new command off the Slider's custom properties if any.

        The legacy ``axis_cmd_seq`` monotonic counter is preserved so
        MCP users driving the axis via raw custom-property writes
        continue to work. Direct Python API callers bypass this path.
        """
        obj = self.rotator.obj
        try:
            seq = int(obj.get("axis_cmd_seq", 0))
        except (TypeError, ValueError):
            seq = 0
        if seq <= self._last_seq:
            return
        self._last_seq = seq

        action = obj.get("axis_cmd_action", "idle")
        if action not in VALID_ACTIONS:
            return  # Unknown action: ignore. ``reset_collision`` 被故意
                    # 排除在 ``VALID_ACTIONS`` 之外 —— 它走
                    # ``CollisionEngine.clear_collision`` 而不是模块的
                    # ``axis_cmd_*`` 队列。

        if action == "idle":
            self.idle()
        elif action == "stop":
            self.stop()
        elif action == "home":
            try:
                direction = int(obj.get("axis_home_direction", -1))
                velocity = float(obj.get("axis_cmd_velocity", 0.1))
            except (TypeError, ValueError):
                direction = -1
                velocity = 0.1
            self.home(direction=direction, velocity=velocity)
        elif action == "move_to":
            try:
                target = float(obj.get("axis_cmd_target_angle", 0.0))
            except (TypeError, ValueError):
                target = 0.0
            # 优先读 ``axis_cmd_velocity``（deg/s）—— UI 的 Move To 按钮与
            # ``axis_ops.send_command`` 写的正是这个字段；只有调用方只写了
            # ``axis_cmd_duration_s`` 时才退回它。
            # （此前这里只读 duration_s，导致 UI 传的速度被忽略，实际按
            # 1 deg/s 慢转。）
            raw_vel = obj.get("axis_cmd_velocity", None)
            velocity = None
            if raw_vel is not None:
                try:
                    velocity = float(raw_vel)
                except (TypeError, ValueError):
                    velocity = None
            if velocity is None or velocity == 0.0:
                try:
                    velocity = float(obj.get("axis_cmd_duration_s", 1.0))
                except (TypeError, ValueError):
                    velocity = 1.0
            self.move_to(target, velocity)
        elif action == "velocity":
            try:
                v = float(obj.get("axis_cmd_velocity", 0.0))
            except (TypeError, ValueError):
                v = 0.0
            self.set_velocity(v)

    def _consume_cmd_blocked(self) -> None:
        """BLOCKED 期间只推进 ``axis_cmd_seq``,不执行任何命令。

        与 :meth:`LinearAxis._consume_cmd_blocked` 同构。``reset_collision``
        不在 ``axis_cmd_*`` 协议里(走 :meth:`CollisionEngine.clear_collision`),
        所以这里不执行任何 axis action,只是把 ``_last_seq`` 推到最新,
        防止下一 tick 重读同一个 seq。
        """
        if self.rotator is None:
            return
        obj = self.rotator.obj
        try:
            seq = int(obj.get("axis_cmd_seq", 0))
        except (TypeError, ValueError):
            seq = 0
        if seq <= self._last_seq:
            return
        self._last_seq = seq

    # ---- per-tick work ----

    def _update_sensors(self) -> None:
        source = self.shim.obj if self.shim is not None else self.rotator.obj
        for s in self.sensors:
            s.update(source)

    def _update_rotator(self, dt: float) -> None:
        self.rotator.update(dt)

    def _update_slider(self, dt: float) -> None:
        self._update_rotator(dt)

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块命中 → 进 BLOCKED(集中式拉模式回调)。"""
        self._enter_blocked_state()

    def on_collision_cleared(self) -> None:
        """碰撞解除 / 引擎被禁用 → 退出 BLOCKED(集中式拉模式回调)。"""
        self._clear_blocked_state_only()

    def _enter_blocked_state(self) -> None:
        """Force the axis into ``STATE_BLOCKED`` and stop the rotator.

        Idempotent. Mirrors :meth:`LinearAxis._enter_blocked_state`.
        """
        if self.state == STATE_BLOCKED:
            return
        if self.rotator is not None:
            try:
                self.rotator.start_idle()
            except Exception:
                pass
        self.state = STATE_BLOCKED
        self._stop_reason = STOP_REASON_COLLISION

    def _clear_blocked_state_only(self) -> None:
        """Drop ``STATE_BLOCKED`` locally without touching the engine.

        Mirrors :meth:`LinearAxis._clear_blocked_state_only` — see
        that method for the auto-unblock rationale.
        """
        if self.state != STATE_BLOCKED:
            return
        if self.rotator is not None:
            try:
                self.rotator.start_idle()
            except Exception:
                pass
        self.state = STATE_IDLE
        self._stop_reason = ""

    def _check_transitions(self) -> None:
        """Apply exit conditions on top of the slider's update.

        Limit sensors (``pos_limit`` / ``neg_limit``) act as soft
        stops during ``MOVING_VEL``: hitting them transitions the axis
        to ``STOPPED_AT_LIMIT`` with ``stop_reason="limit"``.
        """
        if self.state == STATE_HOMING:
            current_dir = 1 if self.rotator.velocity >= 0 else -1
            # Reverse direction if a limit sensor is hit while homing.
            # Limit reversal overrides back-off: a reverse on a limit
            # already leaves the rotator heading back toward home, so
            # we drop the back-off flag and let normal homing take over.
            for s in self.sensors:
                if not s.is_triggered:
                    continue
                if s.kind == "neg_limit" and current_dir < 0:
                    self._home_backoff = False
                    self._home_origin_passed = False
                    self.rotator.start_homing(direction=-current_dir,
                                               velocity=self._home_command_velocity)
                    return
                if s.kind == "pos_limit" and current_dir > 0:
                    self._home_backoff = False
                    self._home_origin_passed = False
                    self.rotator.start_homing(direction=-current_dir,
                                               velocity=self._home_command_velocity)
                    return

            # Back-off sub-state: ``home()`` was called while the home
            # sensor was already on, so the rotator is currently
            # moving in the OPPOSITE direction. Keep moving until the
            # home sensor releases, then switch to the requested
            # direction. Limit-reversal above still applies during
            # back-off.
            if self._home_backoff:
                if not self._is_home_sensor_triggered():
                    self._home_backoff = False
                    self._home_origin_passed = False
                    self.rotator.start_homing(
                        direction=self._home_command_direction,
                        velocity=self._home_command_velocity,
                    )
                return

            for s in self.sensors:
                if s.is_home:
                    if s.is_triggered:
                        if current_dir == self._home_command_direction:
                            current = float(self.rotator.current_angle)
                            self.home_angle = current
                            self.zero_offset = current
                            self.home_done = True
                            self.rotator.start_idle()
                            self.state = STATE_IDLE
                            self._stop_reason = "homed"
                            return
                        self._home_origin_passed = True
                    elif self._home_origin_passed:
                        self.rotator.start_homing(direction=self._home_command_direction,
                                                   velocity=self._home_command_velocity)

                    return

        elif self.state == STATE_MOVING_P2P:
            v = self.rotator.velocity
            for s in self.sensors:
                if not s.is_triggered:
                    continue
                if s.kind == "neg_limit" and v < 0:
                    self.rotator.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return
                if s.kind == "pos_limit" and v > 0:
                    self.rotator.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return

            if self.rotator.consume_arrived():
                self.state = STATE_IDLE
                self._stop_reason = "arrived"

        elif self.state == STATE_MOVING_VEL:
            v = self.rotator.velocity
            for s in self.sensors:
                if not s.is_triggered:
                    continue
                # Polarity-gated: neg_limit kills negative motion,
                # pos_limit kills positive motion.
                if s.kind == "neg_limit" and v < 0:
                    self.rotator.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return
                if s.kind == "pos_limit" and v > 0:
                    self.rotator.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return

    # ---- state property bridge ----

    def _write_state_props(self) -> None:
        """Mirror axis state into the Slider's custom properties.

        These ``axis_*`` properties are the read-side surface for MCP
        ``execute_code`` callers. Writing them is never the way to
        drive the axis (use the Python API or ``axis_cmd_*``).
        """
        obj = self.rotator.obj
        current = float(self.rotator.current_angle - self.zero_offset)
        try:
            from modules.components.geometry import normalize_angle_deg
        except ImportError:  # pragma: no cover - offline path
            from ..components.geometry import normalize_angle_deg
        displayed = normalize_angle_deg(current)

        obj["axis_state"] = self.state
        obj["axis_current_angle"] = displayed
        has_target = (
            self.state == STATE_MOVING_P2P and self.rotator.target_angle is not None
        )
        obj["axis_has_target"] = bool(has_target)
        if has_target:
            try:
                obj["axis_target_angle"] = normalize_angle_deg(
                    float(self.rotator.target_angle) - self.zero_offset
                )
            except Exception:
                obj["axis_target_angle"] = float(self.rotator.target_angle)
        else:
            obj["axis_target_angle"] = 0.0
        obj["axis_velocity"] = float(self.rotator.velocity)
        obj["axis_moving"] = bool(self.state in (
            STATE_HOMING, STATE_MOVING_P2P, STATE_MOVING_VEL
        ))
        obj["axis_home_done"] = bool(self.home_done)
        if self.home_angle is not None:
            obj["axis_home_angle"] = normalize_angle_deg(
                float(self.home_angle) - self.zero_offset
            )
        obj["axis_stop_reason"] = self._stop_reason
        # Aggregate sensor states as a compact JSON string for a
        # single-read API.
        sensors_dict = {s.name: bool(s.is_triggered) for s in self.sensors}
        obj["axis_sensors"] = json.dumps(sensors_dict, separators=(",", ":"))


# Re-export under the legacy name for backwards compatibility with
# `tests/test_framework.py` and any external callers that imported
# ``RotateAxisRuntime`` directly from this module.
__all__ = [
    "RotateAxisRuntime",
    "ALL_STATES",
    "STATE_IDLE",
    "STATE_HOMING",
    "STATE_MOVING_P2P",
    "STATE_MOVING_VEL",
    "STATE_STOPPED_AT_LIMIT",
    "STATE_BLOCKED",
    "VALID_ACTIONS",
]