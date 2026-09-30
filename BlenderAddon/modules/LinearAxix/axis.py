"""LinearAxis: passive axis runtime owned by :class:`SimulationManager`.

This is the V0_3 analogue of the old
:mod:`LinearAxis.core.linear_axis` ``LinearAxisCoordinator``. It is
deliberately **passive**: it owns no timers, no threads, and no
discovery. The owning :class:`SimulationManager` calls
:meth:`update` once per simulation tick.

Each axis bundles:
- one :class:`SliderComponent` (the moving body)
- one :class:`RailComponent` (the travel envelope, used for clamping)
- N :class:`SensorComponent` (AABB-overlap detectors)
- zero or one :class:`TriggerShimComponent` (clean AABB source for sensors)

Public API
----------
Commands (callable directly, or via the legacy ``axis_cmd_*`` custom
property bridge that :meth:`_consume_cmd` reads on every tick):

- :meth:`move_to(target_x, duration_s)`  — point-to-point motion
- :meth:`set_velocity(v)`                — constant-velocity motion
- :meth:`home(direction)`                — homing toward ``-X`` (default)
- :meth:`stop`                           — immediate cancel
- :meth:`idle`                           — alias of :meth:`stop`
- ``reset_collision``                    — clear the scene-level
  ``motion_simulation_collision`` flag and unblock this axis. Only
  command honoured while the axis is in ``STATE_BLOCKED``.

State machine
-------------
``IDLE``           — not moving
``HOMING``         — constant-velocity toward the home sensor
``MOVING_P2P``     — interpolating toward a target X
``MOVING_VEL``     — integrating a constant velocity
``STOPPED_AT_LIMIT`` — terminal: hit a ``front_limit`` / ``back_limit``
                       sensor (or rail clamp) while moving in ``vel`` mode
``BLOCKED``         — terminal: slider contacted a non-axis obstacle.
                       Axis refuses every command except ``reset_collision``.

Limit sensors (``front_limit`` / ``back_limit``) terminate
``MOVING_VEL`` early with ``stop_reason="limit"``. The rail clamp
inside :class:`SliderComponent` remains as a hard fallback for scenes
without explicit limit sensors.

Collision detection
-------------------
On every tick the runtime consults the shared
:class:`CollisionEngine` (passed in as ``collision_engine=``) to see
whether the slider's current world position overlaps any obstacle
mesh. A hit drives the axis into ``STATE_BLOCKED`` with
``stop_reason="collision"`` and writes
``scene["motion_simulation_collision"] = True``. **All** axes block on
the next tick because the scene-level flag is the single source of
truth. The only way to clear the flag is a ``reset_collision`` command
to any axis; the next tick then releases every blocked axis.
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

try:
    from ...framework import BaseSimulationModule, SimulationCommand, is_object_alive
    from ..components.collision import STOP_REASON_COLLISION, module_self_check
    from ..components.sensor.base_sensor import aggregate_axis_sensor_states
    from ._units import scene_linear_bu_to_mm_factor as _bu_to_mm
except ImportError:  # pragma: no cover - offline / direct-script import path
    from framework import BaseSimulationModule, SimulationCommand, is_object_alive  # noqa: F401
    from modules.components.collision import (  # noqa: F401
        STOP_REASON_COLLISION,
        module_self_check,
    )
    from modules.components.sensor.base_sensor import aggregate_axis_sensor_states  # noqa: F401
    from modules.LinearAxix._units import scene_linear_bu_to_mm_factor as _bu_to_mm  # noqa: F401


# State constants. ``stopped_at_limit`` is distinct from ``idle`` so
# callers can tell the slider halted because it ran out of travel
# rather than because someone asked it to stop. ``blocked`` is set by
# the collision engine and cleared only by ``reset_collision``.
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


class LinearAxis(BaseSimulationModule):
    """Passive runtime for one linear axis, exposed as a simulation module."""

    __slots__ = (
        "axis_id",
        "host_obj",
        "slider",
        "rail",
        "shim",
        "sensors",
        "state",
        "home_done",
        "home_x",
        "zero_offset",
        "_home_command_direction",
        "_home_command_velocity",
        "_home_origin_passed",
        "_last_seq",
        "_stop_reason",
        "_collision_engine",
        "category",
        "_geometry_cache_hash",
    )

    def __init__(self, host_obj, *, slider, rail, sensors,
                 shim=None, axis_id: Optional[str] = None,
                 collision_engine=None):
        super().__init__(module_id=axis_id if axis_id is not None else host_obj.name,
                         kind="linear_axis",
                         category="axes")
        self.host_obj = host_obj
        self.axis_id = self.module_id
        self.slider = slider
        self.rail = rail
        self.shim = shim
        self.sensors: List = list(sensors)
        # Slider needs the rail for clamp/limit.
        if rail is not None:
            self.slider.attach_rail(rail)
        # Geometry cache fingerprint: 由 :mod:`modules.LinearAxix.geometry_watch`
        # 的 depsgraph_update_post 钩子写入与比对。
        # ``None`` = 尚未观察过;首次观察时只盖章不刷新(避免初始化完
        # 立刻重算一次)。
        self._geometry_cache_hash: Optional[Tuple] = None
        # Runtime state. The slider's ``current_x`` is the displacement
        # from the slider's own logical origin (``self.slider._start_position``).
        # The origin is captured on construction (= slider's current
        # world position) and updated to the home position after a
        # successful home (so ``current_x == 0`` always means "at the
        # home / start position"). ``home_x`` is the absolute world
        # position of the most recent home, kept for inspection.
        self.state: str = STATE_IDLE
        self.home_done: bool = False
        self.home_x: Optional[float] = None
        self._home_command_direction: int = -1
        self._home_command_velocity: float = 0.1
        self._home_origin_passed: bool = False
        # When entering home() while the home sensor is already on,
        # this axis first drives the slider OPPOSITE to the requested
        # direction until the sensor releases, then resumes homing.
        # See :meth:`home` + the HOMING branch of
        # :meth:`_check_transitions`.
        self._home_backoff: bool = False
        # Initialise ``_last_seq`` to the slider's current command
        # seq so a refresh-built axis does NOT replay any
        # move_to / velocity / home command the previous axis had
        # already consumed. The old ``-1`` default caused a visible
        # "jump" right after Refresh: the new axis's first tick
        # would re-consume seq 0 (or higher), treating the now-stale
        # ``axis_cmd_action / axis_cmd_target_x`` as fresh input and
        # re-driving the slider toward the old target.
        try:
            self._last_seq: int = int(self.slider.obj.get("axis_cmd_seq", 0))
        except (TypeError, ValueError):
            self._last_seq = 0
        self._stop_reason: str = ""
        # Shared :class:`CollisionEngine`. ``None`` means the addon was
        # constructed without a collision engine (test harness, legacy
        # code path) — collision checks are skipped silently in that
        # case, all other behaviour is identical.
        self._collision_engine = collision_engine
        # Push initial state to custom properties so the first external
        # read sees a well-formed schema even before the first tick.
        self._write_state_props()

    # ---- public: geometry-cache refresh ----

    def refresh_geometry_caches(self) -> bool:
        """重算 ``rail.direction`` / ``rail.center`` / ``x_min`` / ``x_max``,
        以及 ``slider._start_position``,基于**当前**世界变换。

        ``RailComponent`` 与 ``SliderComponent`` 在轴发现时一次算好方向
        / 中心 / 逻辑原点;之后父级链一动 (e.g. ``LinearAxisX`` 挂在
        ``Slide.Y`` 上,Slide.Y 被拖到新位置),缓存就过时,slider 沿错误
        世界方向走。本方法由 :mod:`modules.LinearAxix.geometry_watch`
        的 ``depsgraph_update_post`` 钩子在检测到 host/rail matrix 变化
        时调用。

        不变量 (不能被破坏):
        - :attr:`state` 状态机状态 (``IDLE`` / ``HOMING`` / ...) **不变**;
        - :attr:`home_done` / :attr:`home_x` 不变;
        - 当前 motion 命令与 ``_last_seq`` 不变 (因此 P2P 中途被刷新后,
          slider 会接着朝同一目标推,不会被打断);
        - sensor 的 ``is_triggered`` 缓存不变 (下一 tick 重算)。

        Returns
        -------
        bool
            ``True`` 表示成功做了任何重算;``False`` 表示 rail 与 slider
            都不可用 / 任何一侧抛异常——这种情况下不能保证缓存是新鲜的。
        """
        ok = False
        if self.rail is not None:
            try:
                ok = bool(self.rail.refresh()) or ok
            except Exception as exc:
                print(
                    f"[MotionSimulation][LinearAxis][{self.axis_id}] "
                    f"rail.refresh() failed: {exc!r}"
                )
        if self.slider is not None:
            try:
                self.slider.refresh_origin_from_current()
                ok = True
            except Exception as exc:
                print(
                    f"[MotionSimulation][LinearAxis][{self.axis_id}] "
                    f"slider.refresh_origin_from_current() failed: {exc!r}"
                )
        return ok

    # ---- public: commands (Python API) ----

    def move_to(self, target_x: float, velocity: float = 1.0) -> None:
        """Begin point-to-point motion. Target is clamped to rail envelope.

        The ``target_x`` parameter is interpreted as a displacement
        from the slider's current logical origin
        (``self.slider._start_position``). The origin is the slider's
        physical world position on construction, and the home
        position after a successful home — so ``move_to(0)`` returns
        to where the slider was when the axis was built (or where
        it was homed), and ``move_to(N)`` moves N units along the
        rail direction.
        """
        if self.state == STATE_BLOCKED:
            return  # Refuse new commands until reset_collision.
        # The rail's clamp operates on absolute coordinates, so we
        # convert the relative target to absolute by adding the
        # origin's position along the rail direction. The slider's
        # ``_start_position`` is the world position of origin.
        # absolute_target = float(target_x)
        # if self.rail is not None and self._rail_in_direction() is not None:
            # sp = self.slider._start_position
            # d = self._rail_in_direction()
            # try:
                # origin = (
                    # sp[0] * d[0] + sp[1] * d[1] + sp[2] * d[2]
                # )
                # absolute_target = origin + float(target_x)
            # except Exception:
                # pass
        target = float(target_x)
        self.slider.start_move_to(target, float(velocity))
        self.state = STATE_MOVING_P2P
        self._stop_reason = ""

    def set_velocity(self, v: float) -> None:
        """Begin constant-velocity motion at ``v`` units/sec (signed)."""
        if self.state == STATE_BLOCKED:
            return
        self.slider.start_velocity(float(v))
        self.state = STATE_MOVING_VEL
        self._stop_reason = ""

    def home(self, direction: int = -1, velocity: float = 0.1) -> None:
        """Begin homing toward ``direction`` (``-1`` default).

        Back-off step
        ---------------
        If the home sensor is already triggered when ``home()`` is
        invoked (e.g. the slider is already sitting at the home
        position, or the user accidentally called :meth:`home` while
        the slider is on top of the sensor), the axis does **not**
        blindly accept the read. It first drives the slider in the
        OPPOSITE direction until the home sensor releases, then
        starts the requested homing in ``direction``. This makes the
        ``home`` command idempotent regardless of where the slider
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
            self.slider.start_homing(
                direction=-self._home_command_direction,
                velocity=self._home_command_velocity,
            )
        else:
            self._home_backoff = False
            self.slider.start_homing(
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
        self.slider.start_idle()
        self.state = STATE_IDLE
        if not self._stop_reason:
            self._stop_reason = "cmd_stop"

    def idle(self) -> None:
        """Alias of :meth:`stop`. ``stop_reason`` is ``"cmd_idle"``."""
        self.slider.start_idle()
        self.state = STATE_IDLE
        if not self._stop_reason:
            self._stop_reason = "cmd_idle"

    def reset_collision(self) -> None:
        """Clear the scene-level collision flag and release this axis.

        Called by the command pipeline when ``axis_cmd_action ==
        "reset_collision"``. Idempotent: safe to call when the axis is
        not currently blocked. The scene-level flag is cleared through
        the engine so every other blocked axis unblocks on its next
        tick.
        """
        if self._collision_engine is not None:
            self._collision_engine.clear_collision()
        if self.state == STATE_BLOCKED:
            self.slider.start_idle()
            self.state = STATE_IDLE
            self._stop_reason = ""

    # ---- public: module API ----

    def _rail_in_direction(self):
        """Return the rail's unit direction as a 3-tuple, or ``None``.

        A small helper used by ``move_to`` to convert between the
        slider's relative ``current_x`` and absolute rail-frame
        coordinates. Returns ``None`` when no rail is attached (the
        caller falls back to the legacy absolute-coordinate path).
        """
        if self.rail is None:
            return (1.0, 0.0, 0.0)
        try:
            d = self.rail.direction
            return (float(d[0]), float(d[1]), float(d[2]))
        except Exception:
            return (1.0, 0.0, 0.0)

    def apply_command(self, cmd: SimulationCommand) -> None:
        """Dispatch a generic command to the axis runtime."""
        action = getattr(cmd, "action", "")
        payload = getattr(cmd, "payload", {}) or {}
        # BLOCKED 期间拒绝所有 motion 命令。 ``reset_collision`` 不是
        # 模块的 action(它在 ``VALID_ACTIONS`` 之外),交由
        # :meth:`CollisionEngine.clear_collision` / ``set_disabled`` 完成。
        # 原本是各分支内部的 ``if self.state == STATE_BLOCKED: return``
        # 静默抹掉调用意图；现在统一在入口处抛出
        # :class:`AxisBlockedError`,由 RPC / UI 调用方识别后报红或返
        # 专门的错误码,并调 ``engine.clear_collision()`` 解锁。
        # 懒导入避免 ``axis.py`` 与 ``axis_errors.py`` 的循环依赖。
        if self.state == STATE_BLOCKED:
            try:
                from ..axis_errors import AxisBlockedError
            except ImportError:  # pragma: no cover - 离线 fallback
                AxisBlockedError = RuntimeError
            raise AxisBlockedError(
                f"LinearAxis {self.axis_id!r} is blocked by collision; "
                f"call CollisionEngine.clear_collision() to clear the block."
            )
        if action == "move_to":
            self.move_to(float(payload.get("target_x", 0.0)), float(payload.get("velocity", 1.0)))
        elif action == "velocity":
            self.set_velocity(float(payload.get("velocity", 0.0)))
        elif action == "jog":
            # Jog：按显式方向 + 速度幅度定速运动，直到撞 limit sensor
            # 或被 stop / 到 rail 端点。
            #
            # 走 ``set_velocity`` 的 MOVING_VEL 路径：
            # * 该路径的 limit 极性已修正（front_limit 挡负向、back_limit
            #   挡正向），与 move_to / home 行为一致；
            # * SliderComponent 在 vel 模式下还有 rail envelope 兜底
            #   （``at_limit``）。
            #
            # 不再把 jog 转成 ``move_to(±1e6)``：RotateAxis 的 ``move_to``
            # 会把 target 归一化到 (-180, 180]，超大 target 会被折叠成一个小
            # 角度，导致 jog “转到 ~80° 就到点停下”。
            direction = int(payload.get("direction", 1))
            velocity = abs(float(payload.get("velocity", 0.0)))
            self.set_velocity(direction * velocity)
        elif action == "home":
            # UI 路径同样走 axis_ops.send_command 写 axis_cmd_* props +
            # 增 axis_cmd_seq,下一 tick 由 _consume_cmd 统一调 axis.home。
            # RPC 路径走同一份代码,保证:
            # * direction / velocity 缺省值取自 host 端 cfg, 与 UI 面板
            #   一致, 不再硬编码 -1 / 0.1;
            # * velocity 走 mm → BU 换算(同 UI);
            # * BLOCKED 期间静默丢弃(同 _consume_cmd_blocked 守门语义,
            #   UI 那边是发 ERROR 报告, RPC 静默返回 ok=true)。
            # * 反转逻辑(_check_transitions)必然在 _consume_cmd 之后
            #   跑, 与 UI 完全同构 —— 这是修“撞 limit 不换向”bug 的关键:
            #   旧路径可能因 _home_command_velocity 单位 / 默认值不对,
            #   造成反转时给的是 0 速度, slider 原地不动, limit 始终
            #   触发,看上去像“没换向”。
            if self.state == STATE_BLOCKED:
                return
            cfg = getattr(self.host_obj, "linear_axis", None)
            cfg_direction = getattr(cfg, "home_direction", -1) if cfg is not None else -1
            cfg_speed_mm = getattr(cfg, "home_speed", 0.1) if cfg is not None else 0.1
            direction = int(payload.get("direction", cfg_direction))
            velocity_mm = float(payload.get("velocity", cfg_speed_mm))
            # 懒导入避免 axis.py <-> axis_ops.py 循环 import(axis_ops 顶层
            # 已经 from .axis import VALID_ACTIONS)。
            from .axis_ops import send_command
            send_command(
                self.axis_id, action="home",
                home_direction=direction, velocity=velocity_mm,
            )
            # 不直接调 self.home() —— 与 UI 同构,等 _consume_cmd 下一 tick
            # 调。
        elif action in {"stop", "idle"}:
            getattr(self, action)()

    def reset(self) -> None:
        """Reset the runtime to the idle state."""
        self.stop()
        self.home_done = False
        self.home_x = None
        self._home_origin_passed = False
        self._home_backoff = False
        self._stop_reason = ""

    def snapshot(self) -> dict:
        """Return a compact state snapshot for the manager.

        ``current_x`` and ``velocity`` are reported in **mm** /
        **mm·s⁻¹** to match the units the caller typed into the
        operator panel / RPC command — the runtime's internal BU
        scalars are converted via :func:`_bu_to_mm`.

        Sensor 状态以 axis-agnostic 的三布尔字段暴露：
        ``home_sensor`` / ``pos_limit_sensor`` / ``neg_limit_sensor``，
        由 :func:`aggregate_axis_sensor_states` 把 LinearAxis 内部的
        ``back_limit``（=pos）/ ``front_limit``（=neg）映射成统一命名。
        多个同类型 sensor 按 OR 聚合。
        """
        bu_to_mm = _bu_to_mm()
        sensors = aggregate_axis_sensor_states(self.sensors)
        return {
            "module_id": self.module_id,
            "kind": self.kind,
            "name": self.axis_id,
            "state": self.state,
            "home_done": self.home_done,
            "current_x": float(self.slider.current_x) * bu_to_mm,
            "velocity": float(self.slider.velocity) * bu_to_mm,
            "stop_reason": self._stop_reason,
            "home_sensor": bool(sensors["home"]),
            "pos_limit_sensor": bool(sensors["pos_limit"]),
            "neg_limit_sensor": bool(sensors["neg_limit"]),
        }

    # ---- public: collision structure declaration ----

    def collision_structure(self):
        """声明 LinearAxis 的碰撞结构(见 :class:`CollisionStructure`)。

        - **members**:host + slider + rail + shim。是真正会参与碰撞 BVH
          的对象 —— engine 会把它们的 BVH 打成一个 per-axis group,
          跨轴 slider 撞到这组才算碰撞。组内任意两个对象**永不**做碰
          撞检测(host 与其子部件、slider 与 rail 都不会互撞)。子树
          展开与其它模块 host 剪枝由 engine 统一处理。
        - **bodies**:slider —— 唯一的运动体。
        - **sensors**:全部 sensor(``home`` / ``back_limit`` /
          ``front_limit``)。Sensor mesh **不**打进 per-axis BVH(它是
          触发器件,不是 obstacle),但名字会被记入
          ``ResolvedStructure.sensor_names``,供 dev 面板 / 跨轴 sensor
          排除使用。Sensor 的触发判定依然走 ``LinearAxis._update_sensors``
          这条独立路径,与 collision engine 完全无关。

        历史背景：早期版本把 sensor 也塞进 members,会让跨轴 slider 在
        经过对方 axis 的 sensor 物理位置时被 engine 误判为撞了对方
        group(实际只是 sensor mesh 占的体积被重叠)。修复后 sensor 与
        obstacle 解耦,任何轴都不再用 sensor mesh 限制其它轴。
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
        for field in ("slider", "rail", "shim"):
            members.append(unwrap_object(getattr(self, field, None)))
        body = unwrap_object(getattr(self.slider, "obj", None))
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
        5. Advance slider motion.
        6. Run BVH-based collision check on the slider's new position;
           on hit, drive the axis into ``STATE_BLOCKED`` and set the
           scene-level marker.
        7. Apply remaining state transitions (homing → home sensor,
           p2p → arrived, vel → limit sensor / rail clamp).
        8. Mirror state back to ``axis_*`` custom properties for
           read-side MCP access.
        """
        if not self._references_alive():
            self._alive = False
            return

        # Auto-unblock: if another axis has already cleared the
        # scene-level marker (via ``reset_collision``), this axis sees
        # ``marker == False`` on its next tick and returns to idle
        # without needing its own ``reset_collision``. The
        # ``reset_collision`` command itself short-circuits this branch
        # by clearing the marker before the consume path runs, so
        # the same axis that issued ``reset_collision`` is also
        # handled correctly.
        if (self._collision_engine is not None
                and not self._collision_engine.read_marker()
                and self.state == STATE_BLOCKED):
            self._clear_blocked_state_only()

        # Step 2: scene-level collision gating. When the engine has
        # written ``motion_simulation_collision = True`` we drop every
        # command except ``reset_collision`` and refuse to advance
        # motion. This is the fast path once an axis has been blocked.
        if self._collision_engine is not None and self._collision_engine.read_marker():
            if self.state != STATE_BLOCKED:
                self._enter_blocked_state()
            self._consume_cmd_blocked()
            try:
                self._write_state_props()
            except Exception:
                pass
            return

        # Normal path: command intake → sensors → motion → collision
        # check → transitions → property mirror.
        self._consume_cmd()
        self._update_sensors()
        self._update_slider(dt)

        # Step 6: 碰撞。
        #
        # 生产路径(集中式拉模式):碰撞由 ``CollisionEngine.step`` 在
        # 每个 tick 的模块循环**之后**统一计算,命中后通过
        # ``on_collision_hit`` 通知本模块进 BLOCKED。模块不再自己发起
        # 碰撞计算 —— 相同逻辑不再抄在每个模块里。
        #
        # 过渡期兼容:引擎没有接管模块名册时(离线测试替身、外部脚本)
        # 保留旧的“模块自检”路径,行为与重构前完全一致(包括“引擎被
        # 禁用时仍然算一次,只是不进 BLOCKED”)。
        if self._collision_engine is not None and self.slider is not None:
            try:
                import bpy
                depsgraph = bpy.context.evaluated_depsgraph_get()
            except Exception:
                depsgraph = None
            body_obj = getattr(self.slider, "obj", None)
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
        if self.slider is None or not is_object_alive(self.slider.obj):
            return False
        if self.rail is not None and not is_object_alive(self.rail.obj):
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
        continue to work. Direct Python API callers (see :meth:`move_to`
        et al.) bypass this path.
        """
        obj = self.slider.obj
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
        elif action == "idle":
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
                target = float(obj.get("axis_cmd_target_x", 0.0))
            except (TypeError, ValueError):
                target = 0.0
            try:
                velocity = float(obj.get("axis_cmd_velocity", 1.0))
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

        ``reset_collision`` **不在** ``axis_cmd_*`` 协议里（它走
        :meth:`CollisionEngine.clear_collision`）,所以这里什么都不做,
        只是更新 ``_last_seq`` 让同一 seq 不被下一 tick 重复读。
        轴释放的机制是 race 让 engine 清 scene marker,下一 tick
        :meth:`update` 的 marker 分支看到 False 后调
        :meth:`_clear_blocked_state_only` 自动释放。
        """
        if self.slider is None:
            return
        obj = self.slider.obj
        try:
            seq = int(obj.get("axis_cmd_seq", 0))
        except (TypeError, ValueError):
            seq = 0
        if seq <= self._last_seq:
            return
        self._last_seq = seq

    # ---- per-tick work ----

    def _update_sensors(self) -> None:
        source = self.shim.obj if self.shim is not None else self.slider.obj
        for s in self.sensors:
            s.update(source)

    def _update_slider(self, dt: float) -> None:
        self.slider.update(dt)

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块命中 → 进 BLOCKED(集中式拉模式回调)。"""
        self._enter_blocked_state()

    def on_collision_cleared(self) -> None:
        """碰撞解除 / 引擎被禁用 → 退出 BLOCKED(集中式拉模式回调)。"""
        self._clear_blocked_state_only()

    def _enter_blocked_state(self) -> None:
        """Force the axis into ``STATE_BLOCKED`` and stop the slider.

        Idempotent. Called both from the per-tick collision check and
        from the scene-marker branch of :meth:`update`. Mirrors the
        structure of the limit-sensor transitions so the rest of the
        runtime (write_state_props, snapshot) sees a well-formed state.
        """
        if self.state == STATE_BLOCKED:
            return
        if self.slider is not None:
            try:
                self.slider.start_idle()
            except Exception:
                pass
        self.state = STATE_BLOCKED
        self._stop_reason = STOP_REASON_COLLISION

    def _clear_blocked_state_only(self) -> None:
        """Drop ``STATE_BLOCKED`` locally without touching the engine.

        Used by the auto-unblock path in :meth:`update` when the
        scene-level marker has already been cleared by another axis
        calling ``reset_collision``. The slider is forced back to
        idle so the next command can take effect immediately.
        """
        if self.state != STATE_BLOCKED:
            return
        if self.slider is not None:
            try:
                self.slider.start_idle()
            except Exception:
                pass
        self.state = STATE_IDLE
        self._stop_reason = ""

    def _check_transitions(self) -> None:
        """Apply exit conditions on top of the slider's update.

        Limit sensors (``front_limit`` / ``back_limit``) act as soft
        stops during ``MOVING_VEL``: hitting them transitions the axis
        to ``STOPPED_AT_LIMIT`` with ``stop_reason="limit"``. The rail
        clamp inside :class:`SliderComponent` remains as a hard fallback
        for scenes without limit sensors.
        """
        if self.state == STATE_HOMING:
            current_dir = 1 if self.slider.velocity >= 0 else -1
            # Reverse direction if a limit sensor is hit while homing.
            # Limit reversal overrides back-off: a reverse on a limit
            # already leaves the slider heading back toward home, so
            # we drop the back-off flag and let normal homing take over.
            for s in self.sensors:
                if not s.is_triggered:
                    continue
                if s.kind == "front_limit" and current_dir < 0:
                    self._home_backoff = False
                    self._home_origin_passed = False
                    self.slider.start_homing(direction=-current_dir, velocity=self._home_command_velocity)
                    return
                if s.kind == "back_limit" and current_dir > 0:
                    self._home_backoff = False
                    self._home_origin_passed = False
                    self.slider.start_homing(direction=-current_dir, velocity=self._home_command_velocity)
                    return

            # Back-off sub-state: ``home()`` was called while the home
            # sensor was already on, so the slider is currently moving
            # in the OPPOSITE direction. Keep moving until the home
            # sensor releases, then switch to the requested direction.
            # Limit-reversal above still applies during back-off.
            if self._home_backoff:
                if not self._is_home_sensor_triggered():
                    self._home_backoff = False
                    self._home_origin_passed = False
                    self.slider.start_homing(
                        direction=self._home_command_direction,
                        velocity=self._home_command_velocity,
                    )
                return

            for s in self.sensors:
                if s.is_home:
                    if s.is_triggered:
                        if current_dir == self._home_command_direction:
                            # Home detected. Re-establish the
                            # slider's logical origin at the home
                            # position so ``current_x == 0`` means
                            # "at home". The slider's world position
                            # is preserved (it doesn't move visually).
                            self.home_x = float(self.slider.current_x)
                            self.home_done = True
                            self.slider.reset_origin()
                            self.slider.start_idle()
                            self.state = STATE_IDLE
                            self._stop_reason = "homed"
                            return
                        self._home_origin_passed = True
                    elif self._home_origin_passed:
                        self.slider.start_homing(direction=self._home_command_direction, velocity=self._home_command_velocity)

                    return

        elif self.state == STATE_MOVING_P2P:
            # 若运动方向上已有 limit sensor 触发，提前停止 P2P 运动
            # （与 velocity 模式一致）。``slider.velocity`` 是带符号的，
            # 符号即行进方向，所以极性判断与 MOVING_VEL 分支完全相同。
            v = self.slider.velocity
            for s in self.sensors:
                if not s.is_triggered:
                    continue
                if s.kind == "front_limit" and v < 0:
                    self.slider.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return
                if s.kind == "back_limit" and v > 0:
                    self.slider.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return

            if self.slider.consume_arrived():
                self.state = STATE_IDLE
                self._stop_reason = "arrived"

        elif self.state == STATE_MOVING_VEL:
            # ``slider.velocity`` 在 vel / p2p 两种模式下都是 **带符号** 的
            # （p2p 由 ``start_move_to`` 按 target 方向写符号），符号即行进方向。
            v = self.slider.velocity
            for s in self.sensors:
                if not s.is_triggered:
                    continue
                # 极性按 discovery 的命名映射：``back_limit`` 是 pos sensor
                # （正方向限位），``front_limit`` 是 neg sensor（负方向限位）。
                # 因此负限位只挡负向运动，正限位只挡正向运动。
                if s.kind == "front_limit" and v < 0:
                    self.slider.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return
                if s.kind == "back_limit" and v > 0:
                    self.slider.start_idle()
                    self.state = STATE_STOPPED_AT_LIMIT
                    self._stop_reason = "limit"
                    return
            # Rail clamp fallback (SliderComponent clamps every step
            # and exposes ``at_limit``).
            if self.slider.at_limit:
                self.slider.start_idle()
                self.state = STATE_STOPPED_AT_LIMIT
                self._stop_reason = "limit"

    # ---- state property bridge ----

    def _write_state_props(self) -> None:
        """Mirror axis state into the Slider's custom properties.

        These ``axis_*`` properties are the read-side surface for MCP
        ``execute_code`` callers. Writing them is never the way to
        drive the axis (use the Python API or ``axis_cmd_*``).
        """
        obj = self.slider.obj
        obj["axis_state"] = self.state
        # ``current_x`` is already a relative displacement from the
        # slider's logical origin (the home / start position).
        # Display in **mm** so external readers (MCP, Dev panel,
        # RPC state pushes) see the same units the user typed
        # in the operator panel — the runtime still works in
        # BU internally, see :func:`axis_ops.send_command`.
        bu_to_mm = _bu_to_mm()
        obj["axis_current_x"] = float(self.slider.current_x) * bu_to_mm
        has_target = (
            self.state == STATE_MOVING_P2P and self.slider.target_x is not None
        )
        obj["axis_has_target"] = bool(has_target)
        obj["axis_target_x"] = (
            float(self.slider.target_x) * bu_to_mm if has_target else 0.0
        )
        obj["axis_velocity"] = float(self.slider.velocity) * bu_to_mm
        obj["axis_moving"] = bool(self.state in (
            STATE_HOMING, STATE_MOVING_P2P, STATE_MOVING_VEL
        ))
        obj["axis_home_done"] = bool(self.home_done)
        # ``axis_home_x`` is the home position relative to the
        # current logical origin. After home, the home IS the
        # origin, so this is always 0 — kept for backward
        # compatibility with external readers.
        if self.home_x is not None:
            obj["axis_home_x"] = 0.0
        obj["axis_stop_reason"] = self._stop_reason
        # Aggregate sensor states as a compact JSON string for a
        # single-read API.
        sensors_dict = {s.name: bool(s.is_triggered) for s in self.sensors}
        obj["axis_sensors"] = json.dumps(sensors_dict, separators=(",", ":"))