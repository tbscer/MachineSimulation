# -*- coding: utf-8 -*-
"""CylinderModule:综合对象 cylinder 的运行时。

设计概述
--------
``CylinderModule`` 是 :class:`BaseSimulationModule` 的子类,
``kind="cylinder"``,``category="axes"``。它**完全独立**实现运动(不
内部包装 LinearAxis),复用 :class:`ApproachSensor` 组件做触发检测,
复用 :class:`TriggerShimComponent` 风格的 touch_shim 引用,以及
共享 :class:`CollisionEngine` 做障碍碰撞阻塞。

关键不变式
----------
1. 运动方向 ``axis_dir`` = ``(approach_sensor_2.world_pos -
   approach_sensor_1.world_pos).normalized()``。两 sensor 世界位置重合
   → discovery 报错。
2. 到位边界 = 目标 approach sensor 的 ``is_triggered`` 为 True。
3. 没有 rail ⇒ 没有 rail envelope 兜底;若 sensor 失效引用 stale,
   :class:`SimulationManager._references_alive` 会清理本模块。

状态机
------
::
    IDLE ──outputs (T,F)|(F,T) & current != target──> MOVING_TO_<target>
    MOVING_TO_<target> ──target sensor triggered──> IDLE
    * ──collision engine hit──> BLOCKED
    BLOCKED ──reset_collision cmd──> IDLE

外部信号映射
------------
- ``(output_1=T, output_2=F)`` → target = 1
- ``(output_1=F, output_2=T)`` → target = 2
- ``(output_1=T, output_2=T)`` 或 ``(F, F)`` → 无目标,保持 IDLE

touch_shim 同步
---------------
本模块**推荐**在 scene 里把 ``touch_shim`` parent 到 ``work_bar`` —— 这种
情形下 parent-child 关系自动让 touch_shim 跟随 work_bar 的 world transform,
runtime 什么都不用做。

如果不是 parent-child(两者是 host 的平级子物体、或 touch_shim 独立),
runtime 会在 ``__init__`` 时记一次初始 world 偏移,每 tick 把 work_bar 的
world 位移以加在 ``touch_shim.location`` 上。这种情形下需要在 Discovery /
scene 构建时保证两者本来就在 host 的同一子级(否则偏移不收敛)。

具体在 update() step 11:
- 若 ``touch_shim.parent == work_bar`` → 跳过手动同步(parent 已经保证跟随)
- 否则 → 手动加 ``velocity_vec`` 到 ``touch_shim.location``(传统路径)

具体在 ``__init__`` 里检测一次,存为 ``self._touch_shim_follows_work_bar``。

collision
---------
本模块与 :class:`LinearAxis` / :class:`RotateAxisRuntime` 共用**同一套**
collision 契约（同一个单例 :class:`CollisionEngine`）:

1. **只检查自身运动体**: 每 tick 用
   ``engine.check_slider_collision(work_bar, depsgraph, axis_id=self.module_id)``
   检查 work_bar 的 BVH 是否与别的 axis / 非轴障碍物重叠。
2. **全局停车**: 命中且引擎启用时进 ``BLOCKED`` + ``stop_reason="collision"``,
   并 ``engine.mark_collision()`` 写 scene 级 marker
   ``scene["motion_simulation_collision"]``。其它模块下一 tick 读到该 marker
   也会进 BLOCKED —— 即“任一模块碰撞、全体停车”。
3. **只认 reset_collision**: marker 置位期间本模块不推进运动,且除
   ``reset_collision`` 之外的命令全部被拒(``set_outputs`` 例外,它只是
   外部控制信号位,不直接产生运动)。
4. **自动解锁**: 别的模块调 ``reset_collision`` 清掉 marker 后,本模块
   下一 tick 自行退出 BLOCKED(``_clear_blocked_state_only()``)。
5. **引擎禁用 = kill switch**: ``engine.is_enabled`` 为 False 时
   ``check_slider_collision`` 仍会被调用(供 Dev panel 显示
   ``last_collision``),但**不会**把本模块锁进 BLOCKED;同时
   ``CollisionEngine.set_disabled(True)`` 会主动调用本模块的
   ``_clear_blocked_state_only()`` 立即释放已锁定的模块。
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

try:
    from ...framework import BaseSimulationModule, SimulationCommand, is_object_alive
    from ...modules.components.approach_sensor import ApproachSensor
    from ...modules.components.collision import (
        CollisionEngine,
        STOP_REASON_COLLISION,
        module_self_check,
    )
except ImportError:  # pragma: no cover —— 离线/直接脚本 import 路径
    from framework import (  # noqa: F401
        BaseSimulationModule,
        SimulationCommand,
        is_object_alive,
    )
    from modules.components.approach_sensor import ApproachSensor  # noqa: F401
    from modules.components.collision import (  # noqa: F401
        CollisionEngine,
        STOP_REASON_COLLISION,
        module_self_check,
    )


# ---- 状态常量 ----

STATE_IDLE = "idle"
STATE_MOVING_TO_1 = "moving_to_1"
STATE_MOVING_TO_2 = "moving_to_2"
STATE_BLOCKED = "blocked"

ALL_STATES = (
    STATE_IDLE,
    STATE_MOVING_TO_1,
    STATE_MOVING_TO_2,
    STATE_BLOCKED,
)

VALID_ACTIONS = (
    "set_outputs",
    "reset_collision",
    "stop",
    "idle",
)

STOP_REASON_NONE = ""
STOP_REASON_ARRIVED = "arrived_at_sensor"
STOP_REASON_CMD_STOP = "cmd_stop"
STOP_REASON_CMD_IDLE = "cmd_idle"
STOP_REASON_NO_TARGET = "no_target"

# 供外部读取的“当前处于 BLOCKED 时可用哪些动作”清单（与 VALID_ACTIONS 的
# 区别：``VALID_ACTIONS`` 是命令面,这里是 BLOCKED 下的白名单）。
# ``set_outputs`` 只是外部控制信号位,不直接产生运动,因此允许写入;
# ``stop`` / ``idle`` 会改变 ``state``,必须拒绝,否则会绕过碰撞锁。
BLOCKED_ALLOWED_ACTIONS = ("reset_collision", "set_outputs")


# ---- helpers ----


def _obj_world_pos(obj) -> Optional[Tuple[float, float, float]]:
    """返回 obj 的 world 位置 ``(x, y, z)``。退化时返回 None。"""
    if obj is None:
        return None
    try:
        loc = obj.location
        # 在 Blender 5.x 里 ``obj.matrix_world.translation`` 也是 3 元组
        try:
            t = obj.matrix_world.translation
            return (float(t[0]), float(t[1]), float(t[2]))
        except Exception:
            pass
        return (float(loc[0]), float(loc[1]), float(loc[2]))
    except Exception:
        return None


def _vec_sub(a, b):
    '''3D 向量计算， a - b'''
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_length(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _vec_normalize(v):
    n = _vec_length(v)
    if n <= 1e-9:
        return (0.0, 0.0, 0.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def _vec_scale(v, s):
    '''scale 计算， v * s'''
    return (v[0] * s, v[1] * s, v[2] * s)


# ---- 主类 -----------------------------------------------------------


class CylinderModule(BaseSimulationModule):
    """综合对象 cylinder 的运行时。"""

    __slots__ = (
        "host_obj",
        "work_bar",
        "touch_shim",
        "approach_sensor_1",
        "approach_sensor_2",
        "_sensor_1",
        "_sensor_2",
        "axis_dir",                # 3-tuple,单位向量
        "_initial_offset",         # touch_shim 相对 work_bar 的 world 偏移(仅 _touch_shim_follows_work_bar=False 时使用)
        "_touch_shim_follows_work_bar",  # True = touch_shim.parent == work_bar,parent-child 自动跟随
        "state",
        "_output_1",
        "_output_2",
        "_velocity_signed",        # 沿 axis_dir 的有符号速度(BU/s)
        "_stop_reason",
        "_collision_engine",
        "_alive",
        "module_id",
        "kind",
        "category",
    )

    def __init__(
        self,
        host_obj,
        *,
        work_bar,
        touch_shim,
        approach_sensor_1,
        approach_sensor_2,
        collision_engine=None,
        module_id: Optional[str] = None,
    ):
        mid = module_id if module_id is not None else host_obj.name
        super().__init__(module_id=mid, kind="cylinder", category="cylinders")
        self.host_obj = host_obj
        self.work_bar = work_bar
        self.touch_shim = touch_shim
        self.approach_sensor_1 = approach_sensor_1
        self.approach_sensor_2 = approach_sensor_2
        self._collision_engine = collision_engine

        # ---- 推导 axis_dir ----
        p1 = _obj_world_pos(approach_sensor_1)
        p2 = _obj_world_pos(approach_sensor_2)
        if p1 is None or p2 is None:
            raise ValueError(
                f"[Cylinder] {mid}: cannot read world_pos of approach sensors"
            )
        delta = _vec_sub(p2, p1)
        if _vec_length(delta) <= 1e-9:
            raise ValueError(
                f"[Cylinder] {mid}: approach_sensor_1 and approach_sensor_2 "
                f"have identical world positions; cannot derive axis_dir"
            )
        self.axis_dir: Tuple[float, float, float] = _vec_normalize(delta)

        # ---- 构造 2 个 ApproachSensor 实例 ----
        self._sensor_1 = ApproachSensor(approach_sensor_1, trigger_obj=touch_shim)
        self._sensor_2 = ApproachSensor(approach_sensor_2, trigger_obj=touch_shim)

        # ---- 记录 touch_shim 相对 work_bar 的初始 world 偏移 ----
        # 两种情形:
        # 1. touch_shim.parent == work_bar:parent-child 关系自动同步,
        #    不需要任何手动代码;update() step 11 跳过同步。
        # 2. touch_shim 是独立对象(或 parent 到其他不随 work_bar 移动的东西):
        #    runtime 每 tick 把 work_bar 的位移以加在 touch_shim.location 上,
        #    保持构造时的 world 偏移。
        # _initial_offset + _touch_shim_follows_work_bar 控制这个分支。
        try:
            follows = bool(
                self.touch_shim is not None
                and getattr(self.touch_shim, "parent", None) is self.work_bar
            )
        except Exception:
            follows = False
        self._touch_shim_follows_work_bar = follows
        wb_pos = _obj_world_pos(work_bar)
        ts_pos = _obj_world_pos(touch_shim)
        if wb_pos is None or ts_pos is None:
            self._initial_offset = (0.0, 0.0, 0.0)
        else:
            self._initial_offset = _vec_sub(ts_pos, wb_pos)

        # ---- 初始 cfg 输出(从 host_obj.cylinder 读) ----
        self._output_1 = False
        self._output_2 = False
        self._sync_outputs_from_cfg()

        # ---- 运行时状态 ----
        self.state: str = STATE_IDLE
        self._velocity_signed: float = 0.0  # BU/s,沿 axis_dir 有符号
        self._stop_reason: str = STOP_REASON_NONE
        self._alive = True

        # 把 cfg 的 trigger_obj 同步成 touch_shim,避免 artist 手动配
        self._sync_trigger_obj_to_cfg()

    # ---- 内部 cfg 同步 ----

    def _sync_outputs_from_cfg(self) -> None:
        """每 tick 把 host.cylinder.output_1/output_2 镜像到 instance。
        既让外部代码可以从 CylinderModule 直接读,又避免 RPC set_outputs
        后 instance 不一致。
        """
        cfg = getattr(self.host_obj, "cylinder", None)
        if cfg is None:
            return
        try:
            self._output_1 = bool(cfg.output_1)
        except Exception:
            pass
        try:
            self._output_2 = bool(cfg.output_2)
        except Exception:
            pass

    def _sync_outputs_to_cfg(self) -> None:
        """instance → cfg 写回(主要用于 snapshot 阶段读取一致)。"""
        cfg = getattr(self.host_obj, "cylinder", None)
        if cfg is None:
            return
        try:
            cfg.output_1 = self._output_1
            cfg.output_2 = self._output_2
        except Exception:
            pass

    def _sync_trigger_obj_to_cfg(self) -> None:
        cfg = getattr(self.host_obj, "cylinder", None)
        if cfg is None:
            return
        try:
            cfg.trigger_obj = self.touch_shim
        except Exception:
            pass

    # ---- public 命令接口 ----

    def set_outputs(self, output_1: bool, output_2: bool) -> None:
        """外部控制信号写入。下个 tick reconcile 到 motion 决策。"""
        self._output_1 = bool(output_1)
        self._output_2 = bool(output_2)
        self._sync_outputs_to_cfg()

    def idle(self) -> None:
        self._velocity_signed = 0.0
        self.state = STATE_IDLE
        self._stop_reason = STOP_REASON_CMD_IDLE

    def stop(self) -> None:
        self._velocity_signed = 0.0
        self.state = STATE_IDLE
        self._stop_reason = STOP_REASON_CMD_STOP

    def reset_collision(self) -> None:
        """清 scene 级 collision marker 并解除本模块的 BLOCKED。

        与 :meth:`LinearAxis.reset_collision` /
        :meth:`RotateAxisRuntime.reset_collision` 同构: 幂等,
        不在 BLOCKED 时也安全。清 marker 是“全体停车”的出口 ——
        其它被锁住的模块下一 tick 会自行解锁。
        """
        if self._collision_engine is not None:
            try:
                self._collision_engine.clear_collision()
            except Exception:
                pass
        self._clear_blocked_state_only()

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块命中 → 进 BLOCKED(集中式拉模式回调)。"""
        self._enter_blocked_state()

    def collision_bodies_active(self) -> bool:
        """只在**运动**时才参与碰撞检测(保留重构前语义)。

        重构前 ``update()`` 在 ``target is None`` 时早退,所以 Cylinder 停在
        目标位置(或两个 output 都为 0)时根本不做碰撞检测。拉模式默认每
        tick 都查,若不保留这个差异,历史几何穿插(如 work_bar 与同场景
        其它 mesh 相交)会让整台设备一开机就 BLOCKED。
        """
        return self.state in (STATE_MOVING_TO_1, STATE_MOVING_TO_2)

    def on_collision_cleared(self) -> None:
        """碰撞解除 / 引擎被禁用 → 退出 BLOCKED(集中式拉模式回调)。"""
        self._clear_blocked_state_only()

    def _enter_blocked_state(self) -> None:
        """把本模块锁进 BLOCKED(纯本地状态变更)。

        幂等。与 :meth:`LinearAxis._enter_blocked_state` /
        :meth:`RotateAxisRuntime._enter_blocked_state` 同构:停下运动,
        写 ``state`` / ``stop_reason``。

        注意:**本方法不写 scene marker**。marker 由“发现碰撞的那一方”
        写 —— 集中式拉模式下是 :meth:`CollisionEngine.step`,过渡兼容
        路径下是 :func:`collision.module_self_check`;marker 分支
        (别的模块撞了 → 本模块跟着停)进入 BLOCKED 时更不该重复写。
        """
        if self.state == STATE_BLOCKED:
            return
        self._velocity_signed = 0.0
        self.state = STATE_BLOCKED
        self._stop_reason = STOP_REASON_COLLISION

    def _clear_blocked_state_only(self) -> None:
        """本地退出 BLOCKED, 不触碰 engine 的 scene marker。

        两条调用路径:
        1. :meth:`update` 顶部的自动解锁 —— marker 已被别的模块的
           ``reset_collision`` 清掉;
        2. :meth:`CollisionEngine.set_disabled` 的强制释放 —— 它按
           **方法名** ``_clear_blocked_state_only`` 遍历 manager 里的
           模块, 这个命名不是可选项, 改名就会导致 Cylinder 在禁用
           collision 后永远停在 BLOCKED。
        """
        if self.state != STATE_BLOCKED:
            return
        self._velocity_signed = 0.0
        self.state = STATE_IDLE
        self._stop_reason = STOP_REASON_NONE

    def apply_command(self, cmd: SimulationCommand) -> None:
        action = getattr(cmd, "action", "")
        payload = getattr(cmd, "payload", {}) or {}
        # BLOCKED 门控:与 ``LinearAxis._consume_cmd_blocked`` 同构 ——
        # 只认 ``reset_collision``(见 BLOCKED_ALLOWED_ACTIONS)。
        # ``stop`` / ``idle`` 会改 state, 必须丢弃; ``set_outputs``
        # 只写外部控制信号位, 允许写入, 等 reset 之后下一 tick 生效。
        if (self.state == STATE_BLOCKED
                and action not in BLOCKED_ALLOWED_ACTIONS):
            return
        if action == "set_outputs":
            o1 = payload.get("output_1", self._output_1)
            o2 = payload.get("output_2", self._output_2)
            self.set_outputs(o1, o2)
        elif action == "reset_collision":
            self.reset_collision()
        elif action == "stop":
            self.stop()
        elif action == "idle":
            self.idle()

    # ---- 决策与运动 ----

    def _decide_target(self) -> Optional[int]:
        """根据 outputs 决定目标工作位置:1 / 2 / None(非法组合)。"""
        o1, o2 = self._output_1, self._output_2
        if o1 and not o2:
            return 1
        if not o1 and o2:
            return 2
        return None  # (T, T) 或 (F, F)

    def _decide_current(self) -> Optional[int]:
        """根据两 approach sensor 的 is_triggered 决定当前位置:1 / 2 / None。
        两 sensor 同时触发 → 1 优先(避免歧义)。
        """
        if self._sensor_1.is_triggered:
            return 1
        if self._sensor_2.is_triggered:
            return 2
        return None

    def _target_sensor(self, target: int):
        if target == 1:
            return self._sensor_1, self.approach_sensor_1
        return self._sensor_2, self.approach_sensor_2

    def reset(self) -> None:
        self.stop()
        self._stop_reason = STOP_REASON_NONE

    def snapshot(self) -> dict:
        """RPC state_push 用 snapshot。

        字段集:
        - ``state``         runtime state(``"idle"`` / ``"moving_to_1"`` /
                            ``"moving_to_2"`` / ``"blocked"``)
        - ``current_state``       位置状态,字符串:
                                ``"approach_sensor_1"`` / ``"approach_sensor_2"`` /
                                ``"unknown"``(两 sensor 均未触发)
        - ``approach_sensor_1``  bool,``sensor_1.is_triggered`` 的镜像
        - ``approach_sensor_2``  bool,``sensor_2.is_triggered`` 的镜像
        - ``output_1/2``         外部控制信号位的镜像
        - ``stop_reason``        停止原因

        不再暴露 ``current_x``(沿 ``axis_dir`` 的位移)与 ``velocity``
        (当前有符号速度)—— 这两个量只是 runtime 的中间状态,与
        ``state`` / ``current_state`` 强相关;客户端关心"在哪里 / 动了没",
        ``state`` + ``current_state`` + 两个 sensor bool 已经足够。
        仍**不**单独暴露 ``current_position`` / ``target_position``
        (int 1/2/None)—— 这些信息已合并到 ``current_state`` 字符串。
        """
        return {
            "module_id": self.module_id,
            "kind": self.kind,
            "name": self.module_id,
            "state": self.state,
            "current_state": self._format_current_state(),
            "approach_sensor_1": bool(self._sensor_1.is_triggered),
            "approach_sensor_2": bool(self._sensor_2.is_triggered),
            "output_1": bool(self._output_1),
            "output_2": bool(self._output_2),
            "stop_reason": self._stop_reason,
        }

    def _format_current_state(self) -> str:
        """把 ``_decide_current()`` 的 1/2/None 映射成对外字符串。

        - 1 → ``"approach_sensor_1"``
        - 2 → ``"approach_sensor_2"``
        - None → ``"unknown"``(两 sensor 都未触发,或冲突)
        """
        cur = self._decide_current()
        if cur == 1:
            return "approach_sensor_1"
        if cur == 2:
            return "approach_sensor_2"
        return "unknown"

    # ---- public: collision structure declaration ----

    def collision_structure(self):
        """声明 Cylinder 的碰撞结构(见 :class:`CollisionStructure`)。

        - **members**:host + work_bar + touch_shim + 两个 approach sensor。
          touch_shim 按设计就贴在 work_bar 上,所以二者必须在同一组,
          否则“一启动就撞自己”。
        - **bodies**:work_bar —— 唯一的运动体。
        """
        try:
            from ...modules.components.collision_structure import (
                CollisionStructure,
                unwrap_object,
            )
        except ImportError:  # pragma: no cover - 离线/直接脚本 import 路径
            from modules.components.collision_structure import (  # type: ignore
                CollisionStructure,
                unwrap_object,
            )

        members: List = [self.host_obj]
        for field in (
            "work_bar",
            "touch_shim",
            "approach_sensor_1",
            "approach_sensor_2",
        ):
            members.append(unwrap_object(getattr(self, field, None)))
        body = unwrap_object(self.work_bar)
        return CollisionStructure(
            module_id=self.module_id,
            host=self.host_obj,
            members=tuple(m for m in members if m is not None),
            bodies=(body,) if body is not None else (),
        )

    def update(self, dt: float) -> None:
        """每个 tick 由 SimulationManager 调用。

        顺序（与 LinearAxis / RotateAxisRuntime 保持一致）:
        1. 引用存活检查（stale 引用 → ``_alive = False``）。
        2. 把 host.cylinder 的 output_1/output_2 镜像到 instance。
        3. 全局 collision 门控: marker 已清 → 自动解锁；marker 置位 →
           进 BLOCKED 并放弃本 tick。
        4. 刷新两个 approach sensor。
        6. 由 outputs 决定 target、由 sensor 决定 current。
        7-12. 非法组合 / 已在目标位置 → IDLE；否则积分 work_bar 位移并
            重读 sensor（可能这一帧到位）。
        13. collision engine 检查: 按节奏重建障碍 BVH 缓存 → 对 work_bar
            做一次检测 → 命中且引擎启用则进 BLOCKED。
        """
        # 1. 引用存活检查
        for ref in (
            self.host_obj, self.work_bar, self.touch_shim,
            self.approach_sensor_1, self.approach_sensor_2,
        ):
            if not is_object_alive(ref):
                self._alive = False
                return

        # 2. 镜像 cfg output_1/output_2 → instance(让 set_outputs 与 panel toggle 都生效)
        self._sync_outputs_from_cfg()

        # 3. 全局 collision 门控 —— 与 LinearAxis / RotateAxisRuntime 同构。
        if self._collision_engine is not None:
            # 3a. 自动解锁: scene marker 已被别的模块的 ``reset_collision``
            #     清掉(或引擎刚被禁用) → 本地退出 BLOCKED,不需要本模块
            #     再自己收一次 reset_collision。
            if (not self._collision_engine.read_marker()
                    and self.state == STATE_BLOCKED):
                self._clear_blocked_state_only()
            # 3b. marker 置位 → 锁住本模块并放弃本 tick。任一模块(含
            #     LinearAxis / RotateAxis)命中都会写这个 scene 级 marker,
            #     所以“任一模块碰撞、全体停车”对本模块同样成立。
            elif self._collision_engine.read_marker():
                if self.state != STATE_BLOCKED:
                    self._enter_blocked_state()
                return

        # 3c. 本模块自身处于 BLOCKED 时同样短路。engine 为 None 的离线/
        #     遗留路径也靠这一条守住锁态。
        if self.state == STATE_BLOCKED:
            return

        # 4. 跑两 sensor 的更新
        self._sensor_1.update()
        self._sensor_2.update()

        # 5. 决定 target / current
        target = self._decide_target()
        current = self._decide_current()

        # 6. 非法组合 → IDLE
        if target is None:
            self.state = STATE_IDLE
            self._velocity_signed = 0.0
            if self._stop_reason not in (
                STOP_REASON_CMD_STOP, STOP_REASON_CMD_IDLE,
            ):
                self._stop_reason = STOP_REASON_NO_TARGET
            return

        # 7. 已在目标位置 → IDLE
        if current == target:
            self.state = STATE_IDLE
            self._velocity_signed = 0.0
            if self._stop_reason not in (
                STOP_REASON_CMD_STOP, STOP_REASON_CMD_IDLE,
                STOP_REASON_NO_TARGET,
            ):
                self._stop_reason = (
                    STOP_REASON_ARRIVED if current is not None
                    else STOP_REASON_NONE
                )
            return

        # 8. 需要运动 → 算方向与速度
        source_sensor, source_sensor_obj = self._target_sensor(1 if target == 2 else 2)
        target_sensor, target_sensor_obj = self._target_sensor(target)
        source_pos = _obj_world_pos(source_sensor_obj)
        target_pos = _obj_world_pos(target_sensor_obj)
        if target_pos is None:
            self.state = STATE_IDLE
            self._velocity_signed = 0.0
            return

        # 方向：从 **touch_shim**（sensor 实际检测的对象）当前位置指向 target
        # sensor。
        #
        # 为什么用 touch_shim 而不是 work_bar
        # -----------------------------------
        # - sensor 的触发判定基于 touch_shim 的 AABB，所以“朝目标运动”
        #   必须以 touch_shim 的位置为准；
        # - work_bar（活塞本体）与 touch_shim 之间有固定偏移（本场景 ~6.9 BU）。
        #   若用 work_bar 算方向，当 work_bar 恰好经过 target sensor 的 Z 值时，
        #   ``delta_to_target · axis_dir`` 会每步翻转符号 → 速度在 +1/-1 之间
        #   震荡，work_bar 原地抖动（实测 wb.z 在 7.5242/7.4242 之间反复，
        #   vel 在 +1.0000/-1.0000 之间反复）；
        # - touch_shim 到达 target sensor 时 sensor 会触发 → 状态机进入 IDLE，
        #   不会震荡（触发窗口 = 感应盒尺寸，远大于每步位移）。
        ref_pos = _obj_world_pos(self.touch_shim)
        if ref_pos is None:
            ref_pos = _obj_world_pos(self.work_bar)  # 兼容 touch_shim 缺失
        if ref_pos is None:
            self.state = STATE_IDLE
            self._velocity_signed = 0.0
            return
        delta_to_target = _vec_sub(target_pos, ref_pos)
        if _vec_dot(delta_to_target, self.axis_dir) >= 0.0:
            dir_sign = 1.0
        else:
            dir_sign = -1.0

        # 速度(BU/s)
        cfg = getattr(self.host_obj, "cylinder", None)
        try:
            speed_bu = float(cfg.target_speed) if cfg is not None else 10.0
        except Exception:
            speed_bu = 10.0
        if speed_bu < 0.0:
            speed_bu = 0.0
        self._velocity_signed = dir_sign * speed_bu

        # 状态 (从 IDLE 进入 MOVING 时清掉之前残留的 stop_reason)
        self.state = (
            STATE_MOVING_TO_1 if target == 1 else STATE_MOVING_TO_2
        )
        if self._stop_reason in (
            STOP_REASON_NO_TARGET, STOP_REASON_CMD_STOP, STOP_REASON_CMD_IDLE,
        ):
            self._stop_reason = ""  # 默认空, 在 _check_transitions / 命中后重写

        # 9. 积分:work_bar 沿 axis_dir 移动
        velocity_vec = _vec_scale(self.axis_dir, self._velocity_signed * dt)
        try:
            self.work_bar.location.x = self.work_bar.location.x + velocity_vec[0]
            self.work_bar.location.y = self.work_bar.location.y + velocity_vec[1]
            self.work_bar.location.z = self.work_bar.location.z + velocity_vec[2]
        except Exception:
            self._alive = False
            return

        # 10. 同步 touch_shim(仅在不跟随 work_bar 时手动同步偏移)
        # 当 touch_shim.parent == work_bar 时,parent-child 关系已经保证
        # touch_shim 随 work_bar 一起移动,这里跳过;手动加 location 反而
        # 会双倍写入(在 parent 局部系里加一份 + 随 parent 动一份)。
        # if not self._touch_shim_follows_work_bar:
        #     try:
        #         self.touch_shim.location.x = (
        #             self.touch_shim.location.x + velocity_vec[0]
        #         )
        #         self.touch_shim.location.y = (
        #             self.touch_shim.location.y + velocity_vec[1]
        #         )
        #         self.touch_shim.location.z = (
        #             self.touch_shim.location.z + velocity_vec[2]
        #         )
        #     except Exception:
        #         self._alive = False
        #         return

        # 11. 再次 sensor 更新(可能这一帧到位)
        self._sensor_1.update()
        self._sensor_2.update()
        if target_sensor.is_triggered:
            self.state = STATE_IDLE
            self._velocity_signed = 0.0
            self._stop_reason = STOP_REASON_ARRIVED

        # 12. 碰撞:生产路径由 ``CollisionEngine.step`` 在每个 tick 的模块
        #     循环之后统一计算并回调 ``on_collision_hit``。下面这段只是
        #     “引擎未接管模块名册”(离线测试替身 / 外部脚本)时的过渡兼容。
        if self._collision_engine is not None and self.work_bar is not None:
            try:
                if module_self_check(
                    self._collision_engine, self, self.work_bar,
                    _get_depsgraph(),
                ):
                    # ``_enter_blocked_state`` 内部会调
                    # ``engine.mark_collision()``(禁用时它自己是 no-op)。
                    self._enter_blocked_state()
            except Exception:
                pass


def _get_depsgraph():
    """取 Blender evaluated depsgraph;无 depsgraph 时返回 None。"""
    try:
        import bpy
        return bpy.context.evaluated_depsgraph_get()
    except Exception:
        return None


__all__ = [
    "CylinderModule",
    "STATE_IDLE",
    "STATE_MOVING_TO_1",
    "STATE_MOVING_TO_2",
    "STATE_BLOCKED",
    "ALL_STATES",
    "VALID_ACTIONS",
    "BLOCKED_ALLOWED_ACTIONS",
]