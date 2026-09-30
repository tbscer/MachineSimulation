# -*- coding: utf-8 -*-
"""VacuumNozzleModule:真空吸嘴的运行时模块。

功能定位
--------
真空吸嘴模仿工业真空吸嘴的两个基本动作:

- **On**:把「感应区内」的物体吸住 —— 设为其子对象,于是吸嘴被别的对象
  (通常是挂载它的轴)带着运动时,被吸住的物体跟着一起走;
- **Off**(默认):把持有的子对象全部拆解,放回场景目录(``parent = None``,
  保留 world 位姿)。

本模块**不产生任何运动**,也不写 ``axis_*`` 属性;它唯一的副作用是
父子关系切换。

感应区
------
感应区是一个虚拟立方体,锚定在 **host 自己**(``VacuumNozzle*`` 对象)
的局部坐标系里 —— 与 ``ApproachSensor`` 完全同构的几何模型:

- 起点 ``working_face_center``(host 局部系)
- 沿 ``normal_axis`` 延伸 ``cube_size[normal_axis]``
- 横截面 ``cube_size[另两维]``

RNA(挂在任意 Object 上,本模块只从 host 读):

- ``vacuum_nozzle_normal_axis``      Enum X/Y/Z,默认 Z
- ``vacuum_nozzle_cube_size``        FloatVector(3),默认 (1, 1, 3)
- ``vacuum_nozzle_working_face_center`` FloatVector(3),默认 (0, 0, 0)

**没有 ``sensor_mesh``** —— 吸嘴自己就是感应区的锚点。

吸附对象如何选出
----------------
**自动扫描场景**:把每个候选对象的 8 个世界 AABB 角点变换到 host 局部系后用
SAT 与立方体求交,相交即为「被感应」。候选对象必须满足:

1. 不是 host 自己;
2. ``type == "MESH"``;
3. **不是 host 的祖先**(不能把挂着自己的载体 / 机器底座吸走);
4. 没有 ``sensor_type`` 自定义属性(传感器属于机器);
5. 名字不是 ``VacuumNozzle*``(别的吸嘴属于机器);
6. 不在**其它模块声明**的机器部件集合里
   (取各模块 ``collision_structure().members`` 的并集,不含子树 ——
   所以「挂在 host 下的工件」仍然是可吸的);
7. 没有被别的真空吸嘴持有(``obj.parent`` 是另一个 ``VacuumNozzle*``)。

> 旧版曾有一个可选的 ``trigger_obj`` 字段(“只吸指定的那一个对象”)。
> 它在新版已**彻底删除** —— 它是个静默陷阱:旧 ``.blend`` 里每个吸嘴都
> 带着这个必填字段的残留,于是自动扫描被静默绕过,表现为“吸不到任何东西”。
> 现在只有一条路径:自动扫描。

状态机
------
::

    DISABLED(=Off) ──set_enabled(True)──> IDLE
    IDLE(On,未吸住) ──感应区内有候选──> HOLDING
    HOLDING(On,已吸住) ──持续吸新的候选(不自动释放)──> HOLDING
    IDLE / HOLDING ──set_enabled(False) / Off──> DISABLED(释放全部)
    * ──collision engine 命中──> BLOCKED
    BLOCKED ──reset_collision / marker 清除──> DISABLED / IDLE

**吸附是 sticky 的**:一旦吸住就一直持有,只有 Off(或 ``force_release()``)
才释放 —— 这就是真实真空吸嘴的语义(「开真空吸住,关真空丢掉」)。
``Off`` 与 ``force_release`` 都不属于运动,因此在 BLOCKED 期间也会执行,
避免工件被永久粘住。

外部信号
--------
- ``set_enabled(bool)`` —— On/Off。立即释放(Off 时)或下一 tick 吸附(On 时)。
- ``force_release()`` —— 立即强制释放全部,BLOCKED 下的应急出口。
- ``idle()`` / ``stop()`` —— ``set_enabled(False)`` 的别名,与 Cylinder 同构。
- ``reset_collision()`` —— 清 scene marker 并解除 BLOCKED。

collision
---------
``members = host + 当前持有的物体``;``bodies = ()``。吸嘴自身不产生运动,
所以不参与碰撞检测,只参与 scene-level 全局阻塞门控(任一模块命中 →
本模块也进 BLOCKED,直到 reset_collision 解锁)。

注意:``held_obj`` **不**写进 ``SimulationManager`` 的 owned-name 缓存
(那是按 ``_AXIS_OBJECT_FIELDS`` 槽位名 + 子树遍历算的,会随 reparent 变旧),
而是每 tick 现算,避免「释放过的工件被永久拉黑」。
"""

from __future__ import annotations

from typing import List, Optional

try:
    from ...framework import BaseSimulationModule, SimulationCommand, is_object_alive
    from ...modules.components.collision import STOP_REASON_COLLISION
    from ...modules.components.vacuum_nozzle import VacuumNozzleSensor
    from .naming import is_host as _is_nozzle_host
except ImportError:  # pragma: no cover —— 离线/直接脚本 import 路径
    from framework import (  # noqa: F401
        BaseSimulationModule,
        SimulationCommand,
        is_object_alive,
    )
    from modules.components.collision import STOP_REASON_COLLISION  # noqa: F401
    from modules.components.vacuum_nozzle import VacuumNozzleSensor  # noqa: F401
    from modules.VacuumNozzle.naming import is_host as _is_nozzle_host  # noqa: F401


# ---- 状态常量 ----

STATE_DISABLED = "disabled"   # Off(真空关闭),默认态
STATE_IDLE = "idle"           # On,但未吸住任何物体
STATE_HOLDING = "holding"     # On,已吸住至少一个物体
STATE_BLOCKED = "blocked"     # 碰撞锁定

ALL_STATES = (
    STATE_DISABLED,
    STATE_IDLE,
    STATE_HOLDING,
    STATE_BLOCKED,
)

# 可经 apply_command 接受的 action 名(与 LinearAxis / Cylinder 同构)
VALID_ACTIONS = (
    "set_enabled",
    "force_release",
    "idle",
    "stop",
    "reset_collision",
)

# BLOCKED 状态下允许的动作白名单(set_enabled / force_release / reset_collision
# 是状态控制信号,允许写入;idle / stop 会改 state,必须拒绝)
BLOCKED_ALLOWED_ACTIONS = (
    "set_enabled",
    "force_release",
    "reset_collision",
)

# 停止原因(vacuum 语义)
STOP_REASON_NONE = ""
STOP_REASON_DISABLED = "disabled"
STOP_REASON_NO_TARGET = "no_target"        # On 但感应区内没有候选
STOP_REASON_HOLDING = "holding"            # 已吸住物体
STOP_REASON_CMD_FORCE_RELEASE = "force_release"

# 写回 host 的自定义属性(供 overlay 着色,不 import 本模块)
FLAG_ON = "vacuum_nozzle_on"
FLAG_HOLDING = "vacuum_nozzle_holding"
FLAG_SENSING = "vacuum_nozzle_sensing"


# ---- helpers ----


def _get_depsgraph():
    """取 Blender evaluated depsgraph;无 depsgraph 时返回 None。"""
    try:
        import bpy
        return bpy.context.evaluated_depsgraph_get()
    except Exception:
        return None


def _get_context_scene():
    """取当前场景;离线/无 bpy 时返回 None。"""
    try:
        import bpy
        return bpy.context.scene
    except Exception:
        return None


# ---- 主类 -----------------------------------------------------------


class VacuumNozzleModule(BaseSimulationModule):
    """真空吸嘴运行时模块。"""

    __slots__ = (
        "host_obj",
        "_sensor",
        "_collision_engine",
        "_scene",
        "_module_source",
        "state",
        "_enabled",
        "_held_objs",
        "_stop_reason",
        "_alive",
        "module_id",
        "kind",
        "category",
    )

    def __init__(
        self,
        host_obj,
        *,
        collision_engine=None,
        module_id=None,
        scene=None,
        module_source=None,
    ):
        """构造真空吸嘴模块。

        :param host_obj: ``VacuumNozzle*`` 对象 —— 既是配置容器,也是感应区
            的锚点,也是被吸物体的新父级。
        :param collision_engine: 共享 :class:`CollisionEngine`(可为 None)。
        :param module_id: 模块 id,默认取 ``host_obj.name``。
        :param scene: 候选扫描用的场景;``None`` 时用 ``bpy.context.scene``。
        :param module_source: 返回已注册模块列表的 callable(用于取「机器部件」
            集合);``None`` 时懒加载 :func:`addon.get_manager`。
        """
        mid = module_id if module_id is not None else getattr(
            host_obj, "name", "VacuumNozzle1"
        )
        super().__init__(module_id=mid, kind="vacuum_nozzle",
                         category="vacuum_nozzles")
        self.host_obj = host_obj
        self._sensor = VacuumNozzleSensor(host_obj)
        self._collision_engine = collision_engine
        self._scene = scene
        self._module_source = module_source
        # 初始 cfg 镜像
        self._enabled = False
        self._sync_from_cfg()
        # 运行时状态。初始 state 由构造时的 cfg ``enabled`` 推出 ——
        # 场景里存的吸嘴可能是 On(``enabled=True``),若一律从 DISABLED
        # 起步,那么注册后的第一帧(还没跑 update)面板会短暂显示 disabled。
        # ``enabled=True`` 时真正的吸附仍然发生在下一个 tick 的扫描里。
        self.state = STATE_IDLE if self._enabled else STATE_DISABLED
        self._held_objs: List = []
        self._stop_reason = (
            STOP_REASON_DISABLED if not self._enabled else STOP_REASON_NO_TARGET
        )
        self._alive = True

    # ---- 只读视图 ----

    @property
    def held_objects(self) -> tuple:
        """当前持有的物体元组(只读快照)。"""
        return tuple(self._held_objs)

    @property
    def is_on(self) -> bool:
        """真空是否处于 On。"""
        return bool(self._enabled)

    # ---- 内部 cfg 同步 ----

    def _sync_from_cfg(self) -> None:
        """每 tick 把 ``host_obj.vacuum_nozzle.enabled`` 镜像到 instance。

        让 panel toggle 与 ``set_enabled()`` 命令两条路径都生效。
        """
        cfg = getattr(self.host_obj, "vacuum_nozzle", None)
        if cfg is None:
            return
        try:
            self._enabled = bool(cfg.enabled)
        except Exception:
            pass

    def _sync_enabled_from_cfg(self) -> None:
        """兼容别名:只镜像 enabled(旧调用点/测试用)。"""
        cfg = getattr(self.host_obj, "vacuum_nozzle", None)
        if cfg is None:
            return
        try:
            self._enabled = bool(cfg.enabled)
        except Exception:
            pass

    def _sync_enabled_to_cfg(self) -> None:
        """instance → cfg 写回(让 RPC / panel 读到一致值)。"""
        cfg = getattr(self.host_obj, "vacuum_nozzle", None)
        if cfg is None:
            return
        try:
            cfg.enabled = self._enabled
        except Exception:
            pass

    # ---- public 命令接口 ----

    def set_enabled(self, enabled: bool) -> None:
        """On/Off 切换。

        Off 立即释放持有的全部物体(拆父子不是运动,BLOCKED 下也执行);
        On 只是置位,吸附在下一个 tick 的感应扫描里发生。
        """
        self._enabled = bool(enabled)
        self._sync_enabled_to_cfg()
        if not self._enabled:
            self._release_all()
            if self.state != STATE_BLOCKED:
                self.state = STATE_DISABLED
            self._stop_reason = STOP_REASON_DISABLED
            self._write_overlay_flags(sensing=False)

    def force_release(self) -> None:
        """立即强制释放全部持有的物体(无 scene condition 检查)。

        可在 BLOCKED 下执行 —— 是 collision 锁定期间的 escape hatch。
        """
        had = bool(self._held_objs)
        if had:
            self._release_all()
        if self.state == STATE_HOLDING:
            # 状态机归位(极端情况下 _held_objs 为空但 state 还是 holding)
            self.state = STATE_IDLE if self._enabled else STATE_DISABLED
        self._stop_reason = STOP_REASON_CMD_FORCE_RELEASE
        self._write_overlay_flags(sensing=False)

    def idle(self) -> None:
        """``idle`` = ``set_enabled(False)``,与 Cylinder 同构。"""
        self.set_enabled(False)

    def stop(self) -> None:
        """``stop`` = ``set_enabled(False)``,与 Cylinder 同构。"""
        self.set_enabled(False)

    # ---- collision 阻塞路径(契约对齐 LinearAxis / Cylinder) ----

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块命中 → 进 BLOCKED(集中式拉模式回调)。"""
        self._enter_blocked_state()

    def on_collision_cleared(self) -> None:
        """碰撞解除 / 引擎被禁用 → 退出 BLOCKED(集中式拉模式回调)。"""
        self._clear_blocked_state_only()

    def _enter_blocked_state(self) -> None:
        """把本模块锁进 BLOCKED(纯本地状态变更)。

        注意:**本方法不写 scene marker**。marker 由「发现碰撞的那一方」写 ——
        集中式拉模式下是 :meth:`CollisionEngine.step`,兼容路径下是
        :func:`collision.module_self_check`;marker 分支(别的模块撞了 →
        本模块跟着停)进入 BLOCKED 时更不该重复写。
        与 LinearAxis / RotateAxisRuntime / CylinderModule 保持同一契约。
        """
        if self.state == STATE_BLOCKED:
            return
        self.state = STATE_BLOCKED
        self._stop_reason = STOP_REASON_COLLISION

    def _clear_blocked_state_only(self) -> None:
        """本地退出 BLOCKED,不触碰 engine 的 scene marker。

        由 :class:`CollisionEngine.set_disabled` 按方法名遍历调用;
        命名 ``_clear_blocked_state_only`` 不是任意的 —— 改名会导致
        模块在禁用 collision 后永远停在 BLOCKED。VacuumNozzle
        这里保持同名契约。
        """
        if self.state != STATE_BLOCKED:
            return
        if not self._enabled:
            self.state = STATE_DISABLED
        elif self._held_objs:
            self.state = STATE_HOLDING
        else:
            self.state = STATE_IDLE
        self._stop_reason = STOP_REASON_NONE

    def reset_collision(self) -> None:
        """清 scene 级 collision marker 并解除本模块的 BLOCKED。"""
        if self._collision_engine is not None:
            try:
                self._collision_engine.clear_collision()
            except Exception:
                pass
        self._clear_blocked_state_only()

    def apply_command(self, cmd: SimulationCommand) -> None:
        """RPC apply_command 入口(action 名 → 方法)。"""
        action = getattr(cmd, "action", "")
        payload = getattr(cmd, "payload", {}) or {}
        # BLOCKED 门控(与 LinearAxis / Cylinder 同构)
        if (self.state == STATE_BLOCKED
                and action not in BLOCKED_ALLOWED_ACTIONS):
            return
        if action == "set_enabled":
            enabled = payload.get("enabled", self._enabled)
            self.set_enabled(enabled)
        elif action == "force_release":
            self.force_release()
        elif action == "idle":
            self.idle()
        elif action == "stop":
            self.stop()
        elif action == "reset_collision":
            self.reset_collision()

    # ---- 候选筛选 ----

    def _resolve_modules(self) -> list:
        """取当前已注册的模块列表(用于算「机器部件」集合)。"""
        if self._module_source is not None:
            try:
                return list(self._module_source())
            except Exception:
                return []
        try:
            from ...addon import get_manager
            mgr = get_manager()
            return list(mgr.all()) if mgr is not None else []
        except Exception:
            return []

    def _machine_members(self) -> set:
        """其它模块**声明**的机器部件集合。

        取各模块 ``collision_structure().members`` 的并集 —— 这是「声明」
        而不是「子树」,所以挂在 host 下的工件仍然可吸(只有真正登记为
        机器部件的 rail / slider / shim / work_bar / 传感器 / 别的 host
        才会被排除)。
        """
        out: set = set()
        for m in self._resolve_modules():
            if m is self:
                continue
            try:
                structure = m.collision_structure()
            except Exception:
                continue
            for obj in getattr(structure, "members", ()) or ():
                if obj is not None:
                    out.add(obj)
        return out

    def _ancestors(self) -> set:
        """host 的祖先集合(含 host 自己)。"""
        out: set = set()
        obj = self.host_obj
        guard = 0
        while obj is not None and guard < 256:
            out.add(obj)
            try:
                obj = getattr(obj, "parent", None)
            except Exception:
                break
            guard += 1
        return out

    def _held_by_other_nozzle(self, obj) -> bool:
        """``obj`` 是否已被别的真空吸嘴持有(不抢别人的子物体)。"""
        try:
            parent = getattr(obj, "parent", None)
            if parent is None or parent is self.host_obj:
                return False
            return bool(_is_nozzle_host(parent))
        except Exception:
            # stale 引用 / mock 缺属性 —— 当作“没被别人持有”,
            # 后续 is_in_area / _attach 会各自堆错。
            return False

    def _is_pickable(self, obj, machine: set, ancestors: set) -> bool:
        """自动扫描模式下的候选判定(规则见模块 docstring)。"""
        if obj is None or obj is self.host_obj:
            return False
        if not is_object_alive(obj):
            return False
        if getattr(obj, "type", None) != "MESH":
            return False
        if obj in ancestors:
            return False
        if obj in machine:
            return False
        # 传感器(approach / u-type / 旧的 vacuum_nozzle 标记)
        try:
            if obj.get("sensor_type", None):
                return False
        except Exception:
            pass
        # 别的真空吸嘴本体
        if _is_nozzle_host(obj):
            return False
        if self._held_by_other_nozzle(obj):
            return False
        return True

    def _iter_candidates(self):
        """产出本 tick 允许被吸附的候选对象(纯自动扫描)。"""
        scene = self._scene if self._scene is not None else _get_context_scene()
        if scene is None:
            return
        machine = self._machine_members()
        ancestors = self._ancestors()
        for obj in getattr(scene, "objects", ()) or ():
            # 逐个堆错:场景里随时可能存在被删除的 stale 引用 /
            # 属性异常的第三方对象,不能让一个坏对象废掉整轮扫描。
            try:
                if self._is_pickable(obj, machine, ancestors):
                    yield obj
            except Exception:
                continue

    # ---- attach / release ----

    def _attach(self, obj) -> bool:
        """把 ``obj`` 设为 host 的子对象,保留当前 world transform。

        三步:
        1) 备份 world matrix。
        2) 设 ``parent = host``(此时 Blender 保持 local transform,obj
           视觉上"瞬移"到 host 局部原点)。
        3) 设 ``matrix_parent_inverse = host.matrix_world.inverted()``,
           然后 ``matrix_world = 备份值`` —— Blender 用逆变换反推 local,
           恢复 world 位姿。
        """
        if obj is None or obj is self.host_obj:
            return False
        if obj in self._held_objs:
            return True
        # 不抢别的吸嘴已持有的物体
        if self._held_by_other_nozzle(obj):
            return False
        # 备份 world matrix
        mw = None
        try:
            mw = obj.matrix_world.copy()
        except Exception:
            mw = None
        try:
            obj.parent = self.host_obj
        except Exception:
            return False
        # 恢复 world transform
        if mw is not None:
            try:
                obj.matrix_parent_inverse = self.host_obj.matrix_world.inverted()
                obj.matrix_world = mw
            except Exception:
                pass
        self._held_objs.append(obj)
        return True

    def _release(self, obj) -> None:
        """把 ``obj`` 从 host 解绑,放回场景目录,保留**当前** world 位姿。

        注意:Blender 里 ``parent = None`` **不**自动保留 world 位姿 ——
        ``matrix_world = parent.matrix_world @ matrix_parent_inverse @
        matrix_basis``,一旦去掉 parent,world 就退化成 ``matrix_basis``,
        也就是**挂上去那一刻**的位姿。吸嘴带着工件跑一段距离后再 Off,
        工件会弹回被吸住的起点 —— 必须备份当前 world 再写回。

        按需求语义,释放后的物体**不**回到原来的父级,而是落到场景目录下。
        """
        if obj is None:
            return
        # 1) 备份当前(被携带到现在的)world matrix
        mw = None
        try:
            mw = obj.matrix_world.copy()
        except Exception:
            mw = None
        # 2) 解绑
        try:
            obj.parent = None
        except Exception:
            pass
        # 3) 清掉残留的 parent-inverse:没有 parent 时它不参与运算,但留着
        #    会让下一次 attach 多一层偏移(与 Blender 的 Clear Parent 一致)
        try:
            obj.matrix_parent_inverse.identity()
        except Exception:
            pass
        # 4) 把 world 位姿写成当前值(而不是 attach 时的值)
        if mw is not None:
            try:
                obj.matrix_world = mw
            except Exception:
                pass
        self._ensure_in_scene(obj)
        try:
            if obj in self._held_objs:
                self._held_objs.remove(obj)
        except Exception:
            pass

    def _release_all(self) -> None:
        """释放全部持有的物体(倒序,先解绑最后吸进来的)。"""
        for obj in list(reversed(self._held_objs)):
            self._release(obj)
        self._held_objs = []

    def _ensure_in_scene(self, obj) -> None:
        """确保释放后的物体挂在场景目录下(而不是无集合的游离状态)。"""
        scene = self._scene if self._scene is not None else _get_context_scene()
        if scene is None:
            return
        try:
            collections = list(getattr(obj, "users_collection", ()) or ())
        except Exception:
            return
        if collections:
            return
        try:
            scene.collection.objects.link(obj)
        except Exception:
            pass

    def _reconcile_held(self) -> None:
        """清掉 stale / 被外部解绑的持有项。

        两种情况:
        1) 已被删除(stale datablock)→ 清空;
        2) ``obj.parent`` 不再是 host(artist 手动 unparent,或别的脚本
           改了 hierarchy)→ 清空。
        """
        if not self._held_objs:
            return
        keep: List = []
        for obj in self._held_objs:
            if not is_object_alive(obj):
                continue
            try:
                if getattr(obj, "parent", None) is not self.host_obj:
                    continue
            except Exception:
                continue
            keep.append(obj)
        self._held_objs = keep

    # ---- 碰撞结构声明 ----

    def collision_structure(self):
        """声明真空吸嘴的碰撞结构(见 :class:`CollisionStructure`)。

        - **members**:host + 当前持有的物体。吸嘴自身与它吸住的东西
          同属一组,永不互撞(吸住的工件跟着吸嘴走,不能被吸嘴自己
          的载体判定为撞上)。
        - **bodies**:空 —— 吸嘴不产生独立运动,自身不参与碰撞检测
          (它的运动由挂载它的轴负责)。

        未被吸住的工件就是普通障碍物,别的轴压上去仍然算撞 —— 没有
        “声明即永久免疫”的工件槽位(旧版的 `trigger_obj` 已删除)。
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
        for obj in self._held_objs:
            members.append(unwrap_object(obj))
        return CollisionStructure(
            module_id=self.module_id,
            host=self.host_obj,
            members=tuple(m for m in members if m is not None),
            bodies=(),
        )

    # ---- overlay 标志写回 ----

    def _write_overlay_flags(self, *, sensing: bool) -> None:
        """把 On / holding / sensing 三位写回 host,供 overlay 着色。"""
        try:
            self.host_obj[FLAG_ON] = bool(self._enabled)
            self.host_obj[FLAG_HOLDING] = bool(self._held_objs)
            self.host_obj[FLAG_SENSING] = bool(sensing)
        except Exception:
            pass

    # ---- per-tick ----

    def update(self, dt: float) -> None:
        """每个 tick 由 SimulationManager 调用。

        顺序:
            1. 引用存活检查(stale 引用 → ``_alive = False``)。
            2. 镜像 cfg.enabled → instance。
            3. **Off 路径**:释放全部 → DISABLED。放在 collision 门控之前,
               因为「拆父子」不是运动,BLOCKED 期间也必须能丢件。
            4. 全局 collision 门控:marker 已清 → 自动解锁;marker 置位
               → 进 BLOCKED 并放弃本 tick。
            5. held 一致性检查(stale / 外部解绑 → 清）。
            6. On 路径:刷新感应区几何 → 扫描候选 → 吸附新的;
               **不自动释放**(sticky)。
            7. 写回 ``vacuum_nozzle_on / _holding / _sensing`` 给 overlay。
        """
        # 1. 引用存活检查
        if not is_object_alive(self.host_obj):
            self._alive = False
            return

        # 2. 镜像 cfg.enabled → instance
        self._sync_from_cfg()

        # 3. Off(=默认):释放全部。先于 collision 门控执行。
        if not self._enabled:
            self._release_all()
            if self.state != STATE_BLOCKED:
                self.state = STATE_DISABLED
            self._stop_reason = STOP_REASON_DISABLED
            self._write_overlay_flags(sensing=False)
            return

        # 4. 全局 collision 门控
        if self._collision_engine is not None:
            marker = False
            try:
                marker = bool(self._collision_engine.read_marker())
            except Exception:
                marker = False
            if not marker and self.state == STATE_BLOCKED:
                self._clear_blocked_state_only()
            elif marker:
                if self.state != STATE_BLOCKED:
                    self._enter_blocked_state()
                self._write_overlay_flags(sensing=False)
                return

        # 4c. 本模块自身处于 BLOCKED 时同样短路
        if self.state == STATE_BLOCKED:
            self._write_overlay_flags(sensing=False)
            return

        # 5. held 一致性检查
        self._reconcile_held()

        # 6. On:刷新感应区几何 + 扫描吸附(sticky,只加不减)
        self._sensor.refresh()
        depsgraph = _get_depsgraph()
        sensing = False
        try:
            for obj in self._iter_candidates():
                try:
                    in_area = self._sensor.is_in_area(obj, depsgraph)
                except Exception:
                    in_area = False
                if not in_area:
                    continue
                sensing = True
                if obj not in self._held_objs:
                    self._attach(obj)
        except Exception:
            sensing = False

        if self._held_objs:
            self.state = STATE_HOLDING
            self._stop_reason = STOP_REASON_HOLDING
        else:
            self.state = STATE_IDLE
            self._stop_reason = STOP_REASON_NO_TARGET

        # 7. 写回 overlay 标志
        self._write_overlay_flags(sensing=sensing)

    # ---- reset / snapshot ----

    def reset(self) -> None:
        """reset 接口:释放全部持有,回到 DISABLED(Off)。"""
        self._release_all()
        self._enabled = False
        self._sync_enabled_to_cfg()
        self.state = STATE_DISABLED
        self._stop_reason = STOP_REASON_DISABLED
        self._write_overlay_flags(sensing=False)

    def snapshot(self) -> dict:
        """RPC state_push 用 snapshot。

        字段集:
        - ``state``            runtime state(``"disabled"`` / ``"idle"`` /
                               ``"holding"`` / ``"blocked"``)
        - ``enabled``          真空 On/Off(True = On),镜像 cfg
        - ``anchor_name``      感应区锚点(就是 host 自己)
        - ``holding``          bool,是否正在吸住物体
        - ``held_count``       当前吸住的对象数量
        - ``held_names``       当前吸住的对象名列表
        - ``sensing``          bool,本 tick 感应区内是否有候选物体
        - ``stop_reason``      停止原因
        """
        # 重新查一次 sensing(snapshot 不应当 mutate state)
        sensing = bool(self._held_objs)
        if not self._held_objs:
            try:
                depsgraph = _get_depsgraph()
                self._sensor.refresh()
                for obj in self._iter_candidates():
                    if self._sensor.is_in_area(obj, depsgraph):
                        sensing = True
                        break
            except Exception:
                sensing = False
        return {
            "module_id": self.module_id,
            "kind": self.kind,
            "name": self.module_id,
            "state": self.state,
            "enabled": bool(self._enabled),
            "anchor_name": (
                getattr(self.host_obj, "name", None)
                if self.host_obj is not None else None
            ),
            "holding": bool(self._held_objs),
            "held_count": len(self._held_objs),
            "held_names": [
                getattr(o, "name", None) for o in self._held_objs
            ],
            "sensing": bool(sensing),
            "stop_reason": self._stop_reason,
        }


__all__ = [
    "VacuumNozzleModule",
    "STATE_DISABLED",
    "STATE_IDLE",
    "STATE_HOLDING",
    "STATE_BLOCKED",
    "ALL_STATES",
    "VALID_ACTIONS",
    "BLOCKED_ALLOWED_ACTIONS",
    "FLAG_ON",
    "FLAG_HOLDING",
    "FLAG_SENSING",
]