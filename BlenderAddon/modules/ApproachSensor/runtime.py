"""ApproachSensorModule: 被动 Approach sensor module 的运行时。

设计概述
--------
``ApproachSensorModule`` 是 :class:`BaseSimulationModule` 的子类,
``kind="approach_sensor"``, ``category="approach_sensors"``。它**完全独立**
实现 approach sensor 几何检测, 不产运动 —— sensor mesh 作为**静态碰撞
对象 (member)** 参与碰撞检测: 其它模块的运动体撞到它时可被感知, 但
sensor 自己不主动自检 (见下方 collision 章节)。

**与 :class:`modules.Cylinder.cylinder.CylinderModule` 的关系**:
本 module 的几何完全沿用 :class:`components.approach_sensor.ApproachSensor`
组件 —— 中心对齐的轴对齐盒 + working_face_center (中心偏移) +
cube_size (长宽高) 配置、SAT 检测。Cylinder module 的 approach_sensor_1 /
approach_sensor_2 同样来自该组件, 共用同一套几何 helpers。

关键不变式
----------
1. **host 就是 sensor mesh 本身**; 虚拟感应盒的坐标系 = host mesh
   的局部系; 扫描全部场景可见 mesh, 任一世界 AABB 与感应盒相交即
   ``is_triggered = True``。
2. **每 tick 自动扫描**: 不持有特定 ``trigger_obj`` pointer, 与原
   :class:`components.approach_sensor.ApproachSensor` 组件的"检测
   指定对象"语义不同 —— 本 module 扫描**全部**场景 mesh (排除
   host 自己和本 sensor 所属 module 的其它对象)。
3. **scan 排除自己** + 排除当前 module 自身的 ancestors (防止把
   挂着自己的载体当成触发物)。
4. **scan 跳过 sensor**: 其它 ApproachSensor module 的 host mesh 不计为
   触发物 (互相不触发)。

树形 AABB 提前剪枝算法
-----------------------
scene 可能有大量 object, 朴素遍历每个 obj 都做 SAT 会很慢。本 module
按以下算法高效扫描:

1. 找出所有 **root object** (parent == None) 作为遍历起点。
2. 计算感应盒的世界 AABB (8 个局部角点 × matrix_world)。
3. 对每个 root:
   - **AABB 粗筛**: 比较 root 的世界 AABB 与感应盒世界 AABB。
   - **不相交** → 跳过整个 root 的子树 (性能关键)。
   - **相交** → 递归处理 root 自身 + children。
4. 递归时每个 obj 同样先做 AABB 粗筛; 子树粗筛不通过直接剪掉。

mesh 候选额外规则:
- 非 MESH 类型不参与触发 (仅做 AABB 粗筛用于剪枝)。
- ``hide_viewport == True`` 的 mesh 不计为触发。
- 是 host 自己 / 是 host 的祖先 → 跳过。
- 是另一个 ApproachSensor module 的 host (``sensor_type == "approach"``) →
  跳过 (sensor 互不触发)。

命中候选后跑 SAT: 把候选的 8 个世界 AABB 角点变换到 sensor 局部系,
与感应盒做 15 轴 SAT 判定。

sensor mesh 作为静态碰撞对象 (member, 非 body)
------------------------------------------
本 module 把 sensor mesh 声明为**组成员** (``members=(host_obj,)``) 但
**不是碰撞 body** (``bodies=()``)。含义:

- **可被撞**: 其它模块的 slider / rotator / work_bar / 真空吸嘴 等运动体
  在自检时, 会看到本 sensor 所在的 group BVH, 重叠即触发全局 collision
  marker → 所有模块进 BLOCKED。即 sensor mesh 是一个“实物”, 会被
  碰撞系统感知。
- **不自检**: sensor 是静态物体 (不运动), 不需要每 tick 查“我撞到了
  谁”。也因此避免了“贴面误报”—— sensor mesh 与相邻静态部件
  (如 CBase) 只要几何上贴在一起 (穿透 ~0.01 µm 级), 若 sensor 自己
  当 body 就会被 BVH (epsilon=0.0) 判成碰撞, 把所有模块锁死。
- sensor mesh 自身的 AABB 即它在组里的碰撞边界; 盒子 (working_face_center
  + cube_size) 是触发检测用的虚拟区域, 与碰撞边界是两套独立几何。

状态机
------

::

    DISABLED  ── enabled (cfg) ──> ACTIVE
    ACTIVE    ── collision engine hit ──> BLOCKED
    BLOCKED   ── reset_collision cmd ──> ACTIVE
    BLOCKED   ── engine disabled ──> ACTIVE   (_clear_blocked_state_only)
    ACTIVE    ── enabled = False (cfg) ──> DISABLED
    *         ── is_object_alive False ──> _alive=False → manager unregister

``is_triggered`` 与 ``triggered_obj_name`` 是**纯感知量** (每 tick
SAT 扫描结果), 不参与状态机。``state`` 仅反映 module 自身生命周期。

外部信号映射
------------
- 本 module **无业务动作** —— 纯被动原件, 只接受 ``reset_collision``
  (与 LinearAxis / Cylinder 同构的全局碰撞解锁), 其它 action 全部
  静默丢弃。

collision
---------
本模块与 LinearAxis / RotateAxis / Cylinder / VacuumNozzle 共用
**同一套** collision 契约 (同一个单例 :class:`CollisionEngine`):

1. **显式声明 collision_structure**: 覆写基类的 fallback, 返回
   ``CollisionStructure(members=(host_obj,), bodies=())`` —— sensor mesh
   被认领 (不会进 ``uncovered``) 且作为**可被撞的静态成员**。
2. **sensor mesh 不作 body**: 它不运动, 不自检; 其它模块的运动体
   撞到 sensor 物理位置时才由“对方”发起检测并触发全局 marker。
3. **组内不互撞**: sensor mesh 在自己组里, 与其 host 永不互撞。
4. **全局停车**: 任一模块命中 → 本模块也进 BLOCKED, 直到
   ``reset_collision`` 解锁。
5. **引擎禁用 = 立即解锁**。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

try:
    from ...framework import BaseSimulationModule, SimulationCommand, is_object_alive
    from ...modules.components.approach_sensor.approach_sensor import (
        ApproachSensor,
        _compute_cube_bounds,
        _oriented_box_overlaps_aabb,
        _trigger_world_corners,
        _world_to_local,
    )
    from ...modules.components.collision import STOP_REASON_COLLISION
    from ...modules.components.collision_structure import CollisionStructure
except ImportError:  # pragma: no cover —— 离线/直接脚本 import 路径
    from framework import (  # noqa: F401
        BaseSimulationModule,
        SimulationCommand,
        is_object_alive,
    )
    from modules.components.approach_sensor.approach_sensor import (  # noqa: F401
        ApproachSensor,
        _compute_cube_bounds,
        _oriented_box_overlaps_aabb,
        _trigger_world_corners,
        _world_to_local,
    )
    from modules.components.collision import STOP_REASON_COLLISION  # noqa: F401
    from modules.components.collision_structure import CollisionStructure  # noqa: F401


# ---- 状态常量 ----

STATE_DISABLED = "disabled"   # cfg.enabled = False
STATE_ACTIVE = "active"       # 正常扫描状态
STATE_BLOCKED = "blocked"     # 碰撞锁定

ALL_STATES = (
    STATE_DISABLED,
    STATE_ACTIVE,
    STATE_BLOCKED,
)

# 可经 apply_command 接受的 action 名 —— **纯被动原件**, 只有
# ``reset_collision`` 一个全局解锁动作与 LinearAxis / Cylinder 同构。
# ``stop`` / ``idle`` 不是业务动作, 但保留接口 (返回 False 静默丢弃),
# 不破 RPC 协议。
VALID_ACTIONS = ("reset_collision",)

# BLOCKED 期间允许的动作白名单 (与 LinearAxis / Cylinder 同构)。
BLOCKED_ALLOWED_ACTIONS = ("reset_collision",)

STOP_REASON_NONE = ""

# 写回 host 的自定义属性 (供 overlay 着色 / 互查)
_FLAG_TRIGGERED = "is_triggered"

# AABB 粗筛的数值 eps (用 1e-7 mm = 0.1 nm, 远小于任何可见 mesh)
_AABB_EPS = 1e-7


# ---- helpers ----


def _get_depsgraph():
    """取 Blender evaluated depsgraph; 无 depsgraph 时返回 None。"""
    try:
        import bpy
        return bpy.context.evaluated_depsgraph_get()
    except Exception:
        return None


def _get_context_scene():
    """取当前场景; 离线/无 bpy 时返回 None。"""
    try:
        import bpy
        return bpy.context.scene
    except Exception:
        return None


def _aabb_intersects(min1, max1, min2, max2, eps: float = _AABB_EPS) -> bool:
    """世界 AABB 相交判定 (两个轴对齐盒是否相交)。

    ``min1/max1`` / ``min2/max2`` 是 ``(x, y, z)`` 元组, 返回 True iff
    两 AABB 在三轴上都有重叠 (允许 eps 内的触碰)。
    """
    if min1[0] > max2[0] + eps or min2[0] > max1[0] + eps:
        return False
    if min1[1] > max2[1] + eps or min2[1] > max1[1] + eps:
        return False
    if min1[2] > max2[2] + eps or min2[2] > max1[2] + eps:
        return False
    return True


def _unwrap_object(obj):
    """解包可能存在的 PointerProperty wrapper / dict 替身 (见 collision_structure)。"""
    try:
        from ...modules.components.collision_structure import unwrap_object
        return unwrap_object(obj)
    except ImportError:  # pragma: no cover
        return obj


# ---- 主类 -----------------------------------------------------------


class ApproachSensorModule(BaseSimulationModule):
    """被动 Approach sensor module 的运行时。

    每 tick 用树形 AABB 提前剪枝 + SAT 精确检测, 扫描场景中所有 mesh,
    命中任一即 ``is_triggered = True``; 同时 sensor mesh 自身作为碰撞
    body 参与全局 collision 检测。
    """

    __slots__ = (
        "host_obj",
        "_collision_engine",
        "state",
        "_enabled",
        "_is_triggered",
        "_triggered_obj_name",
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
        module_id: Optional[str] = None,
    ):
        mid = module_id if module_id is not None else getattr(
            host_obj, "name", "ApproachSensor1"
        )
        super().__init__(
            module_id=mid, kind="approach_sensor", category="approach_sensors"
        )
        self.host_obj = host_obj
        self._collision_engine = collision_engine
        # 初始 cfg 镜像
        self._enabled = True
        self._sync_enabled_from_cfg()
        # 运行时状态 —— 初始 state 由构造时的 cfg ``enabled`` 推出。
        # ``enabled=False`` 时进入 DISABLED, 每 tick 只镜像 cfg + 写
        # overlay flag, 不做 scan。
        self.state = STATE_ACTIVE if self._enabled else STATE_DISABLED
        # scan 结果缓存
        self._is_triggered = False
        self._triggered_obj_name = ""
        self._alive = True

    # ---- 内部 cfg 同步 ----

    def _sync_enabled_from_cfg(self) -> None:
        """每 tick 把 ``host_obj.approach_sensor.enabled`` 镜像到 instance。"""
        cfg = getattr(self.host_obj, "approach_sensor", None)
        if cfg is None:
            return
        try:
            self._enabled = bool(cfg.enabled)
        except Exception:
            pass

    # ---- collision 阻塞路径 (契约对齐 LinearAxis / Cylinder / VacuumNozzle) ----

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块 (或其它模块) 命中 → 进 BLOCKED。

        本 module 的 sensor mesh **不作碰撞 body** (``bodies=()``), 所以这个
        回调**不会**因为本 module 自己撞了什么而触发; 但全局 marker
        任一命中 (包括其它运动体撞到本 sensor) 都会把它一起拉进 BLOCKED。
        """
        self._enter_blocked_state()

    def on_collision_cleared(self) -> None:
        """碰撞解除 / 引擎被禁用 → 退出 BLOCKED。"""
        self._clear_blocked_state_only()

    def _enter_blocked_state(self) -> None:
        """把本模块锁进 BLOCKED (纯本地状态变更, 不写 marker)。"""
        if self.state == STATE_BLOCKED:
            return
        self.state = STATE_BLOCKED

    def _clear_blocked_state_only(self) -> None:
        """本地退出 BLOCKED, 不触碰 engine 的 scene marker。"""
        if self.state != STATE_BLOCKED:
            return
        self.state = STATE_ACTIVE if self._enabled else STATE_DISABLED

    def reset_collision(self) -> None:
        """清 scene 级 collision marker 并解除本模块的 BLOCKED。"""
        if self._collision_engine is not None:
            try:
                self._collision_engine.clear_collision()
            except Exception:
                pass
        self._clear_blocked_state_only()

    def apply_command(self, cmd: SimulationCommand) -> None:
        """RPC apply_command 入口 (纯被动原件, 仅放行 ``reset_collision``)。"""
        action = getattr(cmd, "action", "")
        payload = getattr(cmd, "payload", {}) or {}
        # BLOCKED 门控 (与 LinearAxis / Cylinder / VacuumNozzle 同构)。
        if (self.state == STATE_BLOCKED
                and action not in BLOCKED_ALLOWED_ACTIONS):
            return
        if action == "reset_collision":
            self.reset_collision()
        # 其它 action 全部静默 return —— 纯被动原件不接受业务动作。

    # ---- AABB / 几何 helpers ----

    def _compute_detection_world_aabb(self):
        """计算感应盒的世界 AABB (含 sensor.matrix_world 的旋转 / 平移)。

        返回 ``((min_x, min_y, min_z), (max_x, max_y, max_z))`` 或
        ``(None, None)`` 表示失败。

        算法: 感应盒在 sensor 局部系是 AABB, 8 个角点变换到世界后,
        取 min / max 作为世界 AABB (因为 sensor 可能旋转, 局部 AABB
        在世界不一定轴对齐, 但作为粗筛上下界足够)。
        """
        try:
            mw = self.host_obj.matrix_world
            if mw is None:
                return None, None
        except Exception:
            return None, None
        # 读 cfg (artist 在面板上调 cube_size / face_center 后下一 tick
        # 立即生效 —— 不缓存; 开销可忽略)
        cube_size = (1.0, 1.0, 1.0)
        face_center = (0.0, 0.0, 0.0)
        try:
            sensor = ApproachSensor(self.host_obj, trigger_obj=None)
            cube_size = tuple(sensor.cube_size)
            face_center = tuple(sensor.working_face_center)
        except Exception:
            pass
        cube_min, cube_max = _compute_cube_bounds(cube_size, face_center)
        # 8 个局部角点 → 世界
        try:
            world_corners = [mw @ _Vec(c) for c in (
                (cube_min[0], cube_min[1], cube_min[2]),
                (cube_max[0], cube_min[1], cube_min[2]),
                (cube_min[0], cube_max[1], cube_min[2]),
                (cube_max[0], cube_max[1], cube_min[2]),
                (cube_min[0], cube_min[1], cube_max[2]),
                (cube_max[0], cube_min[1], cube_max[2]),
                (cube_min[0], cube_max[1], cube_max[2]),
                (cube_max[0], cube_max[1], cube_max[2]),
            )]
        except Exception:
            return None, None
        wx = [v[0] for v in world_corners]
        wy = [v[1] for v in world_corners]
        wz = [v[2] for v in world_corners]
        return (min(wx), min(wy), min(wz)), (max(wx), max(wy), max(wz))

    def _compute_world_aabb(self, obj):
        """计算 obj 的世界 AABB (用其 mesh bound_box)。

        返回 ``((min_x, min_y, min_z), (max_x, max_y, max_z))`` 或
        ``(None, None)`` 表示失败 (无 bound_box / 无 matrix_world 等)。
        """
        try:
            bb = obj.bound_box
        except Exception:
            return None, None
        if bb is None or len(bb) < 8:
            return None, None
        try:
            mw = obj.matrix_world
            if mw is None:
                return None, None
            # 直接用 8 个角点的世界坐标算 AABB
            world_corners = [mw @ _Vec(c) for c in bb[:8]]
        except Exception:
            return None, None
        wx = [v[0] for v in world_corners]
        wy = [v[1] for v in world_corners]
        wz = [v[2] for v in world_corners]
        return (min(wx), min(wy), min(wz)), (max(wx), max(wy), max(wz))

    # ---- 树形扫描 (根节点 AABB 粗筛 + 子树递归) ----

    def _is_other_sensor(self, obj) -> bool:
        """``obj`` 是不是另一个 ApproachSensor module 的 host?

        防止互相触发: 两个 sensor 不应把对方当成触发物 (sensor mesh
        本身就**是**感应区, 互相扫描只会造成连锁状态翻转)。
        """
        if obj is None:
            return False
        try:
            return obj.get("sensor_type", None) == "approach"
        except Exception:
            return False

    def _iter_candidates(self):
        """遍历场景中所有 object, 用层级 AABB 提前剪枝。

        详细算法见模块 docstring 的"树形 AABB 提前剪枝算法"小节。
        """
        scene = _get_context_scene()
        if scene is None:
            return
        # ---- 排除集 (host 自身 + 祖先) ----
        ancestors: set = set()
        cur = self.host_obj
        guard = 0
        while cur is not None and guard < 256:
            try:
                ancestors.add(cur)
                cur = getattr(cur, "parent", None)
            except Exception:
                break
            guard += 1
        # ---- 计算感应盒的世界 AABB (粗筛的对照物) ----
        det_min, det_max = self._compute_detection_world_aabb()
        if det_min is None:
            return
        # ---- 找出 root objects, 对每个 root 递归扫描 ----
        for obj in getattr(scene, "objects", ()) or ():
            try:
                if obj is None:
                    continue
                if not is_object_alive(obj):
                    continue
                # 只从 root 出发 (parent is None); parented obj 会在
                # 父节点的递归里被扫到, 不会漏 (root + 整个子树)
                if getattr(obj, "parent", None) is not None:
                    continue
                yield from self._scan_subtree(obj, det_min, det_max, ancestors)
            except Exception:
                # 单个 root 异常不能让整轮扫描失败。
                continue

    def _scan_subtree(self, obj, det_min, det_max, ancestors):
        """递归扫描 obj 及其子树。

        AABB 粗筛规则:
        - obj 的世界 AABB 与感应盒 AABB 不相交 → 整个子树都不可能相交,
          提前剪枝 (return, 不递归 children)。
        - 否则 → 检查 obj 自身是否候选 mesh; 然后递归 children。
        """
        # 1. AABB 粗筛 (关键优化: 子树不相交时跳过递归)
        obj_min, obj_max = self._compute_world_aabb(obj)
        if obj_min is None:
            return
        if not _aabb_intersects(obj_min, obj_max, det_min, det_max):
            return  # 整个子树都不可能相交, 提前剪枝
        # 2. obj 自身可能是候选 mesh
        if (
            obj is not self.host_obj
            and obj not in ancestors
            and not self._is_other_sensor(obj)
            and getattr(obj, "type", None) == "MESH"
            and not getattr(obj, "hide_viewport", False)
        ):
            yield obj
        # 3. 递归 children
        try:
            children = getattr(obj, "children", ()) or ()
        except Exception:
            children = ()
        for child in children:
            try:
                if child is None or not is_object_alive(child):
                    continue
                yield from self._scan_subtree(child, det_min, det_max, ancestors)
            except Exception:
                continue

    def _detect_one(self, candidate, depsgraph) -> bool:
        """对单个候选 mesh 跑一次 SAT (与 ApproachSensor._detect 同构)。

        走 :class:`ApproachSensor` 内部 helpers 但**不**新建 ApproachSensor 实例
        (本 module 已经有 sensor mesh; helper 里只需要 cube bounds + OBB
        几何)。返回 True iff candidate 与感应盒相交。
        """
        if candidate is None:
            return False
        world_corners = _trigger_world_corners(candidate, depsgraph)
        if world_corners is None:
            return False
        # 把 8 个角点变换到 sensor 局部系
        local_corners = []
        for w in world_corners:
            lp = _world_to_local(self.host_obj, w)
            if lp is None:
                return False
            local_corners.append(lp)
        # 读 cube 配置 (每次都读 cfg, 让 artist 调 cube_size 后下一 tick
        # 立即生效 —— 不缓存; 开销可忽略)
        sensor = ApproachSensor(self.host_obj, trigger_obj=None)
        cube_min, cube_max = _compute_cube_bounds(
            sensor.cube_size, sensor.working_face_center
        )
        # 退化保护: 盒任一维度坍缩为零 → 不可能触发
        if (
            (cube_max[0] - cube_min[0]) <= 1e-9
            or (cube_max[1] - cube_min[1]) <= 1e-9
            or (cube_max[2] - cube_min[2]) <= 1e-9
        ):
            return False
        return _oriented_box_overlaps_aabb(
            local_corners, cube_min, cube_max
        )

    # ---- collision structure ----

    def collision_structure(self):
        """声明 ApproachSensor 的碰撞结构 —— sensor mesh 作静态成员 (非 body)。

        - **members**: host + sensor mesh 自身 (sensor mesh 在自己组里,
          与其 host 永不互撞; 同时作为**可被撞的静态成员**)
        - **bodies**: ``()`` —— sensor 是静态物体, 不运动, 不自检。

        为什么不做 body (方案 C)
        -----------------------
        把 sensor mesh 当 body 会让它每 tick 主动查“我撞到了谁”。
        sensor 是静态的, 不需要这层自检; 更重要的是, sensor mesh
        常与相邻静态部件 (如 Cylinder 的 CBase) 几何上贴合 —— 只要
        存在 ~0.01 µm 级的浮点/缩放噪声重叠, Blender 的
        ``BVHTree(epsilon=0.0)`` 就会判为碰撞, 把所有模块锁死
        (实测: ApproachSensor2 ↔ CBase 穿透 0.000017166 mm)。

        放进 ``members`` 后, sensor 仍能被其它模块的**运动体**
        (slider / rotator / work_bar / 真空吸嘴) 检出: 对方自检时
        会撞到本 sensor 所在的 group BVH, 重叠即触发全局 marker。
        这满足“sensor 是实物且参与碰撞系统”的语义, 又不引入贴面误报。

        显式覆写基类的 legacy_structure fallback, 让 sensor mesh 被
        引擎认领 (避免进 ``uncovered``)。
        """
        member = _unwrap_object(self.host_obj)
        return CollisionStructure(
            module_id=self.module_id,
            host=self.host_obj,
            members=(member,) if member is not None else (),
            bodies=(),
        )

    # ---- per-tick ----

    def update(self, dt: float) -> None:
        """每个 tick 由 SimulationManager 调用。

        顺序:
        1. 引用存活检查 (stale 引用 → ``_alive = False``)。
        2. 镜像 cfg.enabled → instance。
        3. disabled 路径: 清 scan 结果, 不做扫描, 不进入 BLOCKED 检查
           (DISABLED 是 user 主动关掉, 不是碰撞锁, 应允许 set_outputs
           以外的命令路径在 DISABLED 时也能进 cfg 写回)。返回。
        4. 全局 collision 门控: marker 已清 → 自动解锁; marker 置位
           → 进 BLOCKED 并放弃本 tick。
        5. BLOCKED 短路 (engine 为 None 的兼容路径)。
        6. 业务逻辑: 用树形 AABB 剪枝扫描候选 mesh, 找到第一个 SAT
           命中; 状态翻转时写回 ``host["is_triggered"]``。
        """
        # 1. 引用存活检查
        if not is_object_alive(self.host_obj):
            self._alive = False
            return

        # 2. 镜像 cfg.enabled → instance
        self._sync_enabled_from_cfg()

        # 3. disabled 路径 —— 不做 scan, 写回 OFF 状态, 直接返回
        if not self._enabled:
            self._is_triggered = False
            self._triggered_obj_name = ""
            try:
                self.host_obj[_FLAG_TRIGGERED] = False
            except Exception:
                pass
            if self.state != STATE_BLOCKED:
                self.state = STATE_DISABLED
            return

        # 4. 全局 collision 门控 —— 与 LinearAxis / Cylinder 同构。
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
                return

        # 4c. 本模块自身处于 BLOCKED 时同样短路
        if self.state == STATE_BLOCKED:
            return

        # 5. 业务逻辑: 树形 AABB 剪枝扫描 + SAT 精确检测
        depsgraph = _get_depsgraph()
        new_triggered = False
        new_name = ""
        for candidate in self._iter_candidates():
            try:
                hit = self._detect_one(candidate, depsgraph)
            except Exception:
                hit = False
            if hit:
                new_triggered = True
                try:
                    new_name = getattr(candidate, "name", "") or ""
                except Exception:
                    new_name = ""
                break  # 第一个命中即返回, 与 ApproachSensor 语义对齐

        # 6. 状态翻转写回 (与 ApproachSensor 组件同约定)
        if new_triggered != self._is_triggered:
            self._is_triggered = new_triggered
            try:
                self.host_obj[_FLAG_TRIGGERED] = new_triggered
                try:
                    import bpy
                    self.host_obj["approach_last_change_frame"] = int(
                        bpy.context.scene.frame_current
                    )
                except Exception:
                    self.host_obj["approach_last_change_frame"] = -1
            except Exception:
                pass
        self._triggered_obj_name = new_name if new_triggered else ""

        # 7. state 一致化 (DISABLED → ACTIVE 转换)
        if self.state == STATE_DISABLED:
            self.state = STATE_ACTIVE

    # ---- reset / snapshot ----

    def reset(self) -> None:
        """reset 接口: 清 scan 状态, 回到 ACTIVE (cfg 保留)。"""
        self._is_triggered = False
        self._triggered_obj_name = ""
        try:
            self.host_obj[_FLAG_TRIGGERED] = False
        except Exception:
            pass
        self.state = STATE_ACTIVE if self._enabled else STATE_DISABLED

    def snapshot(self) -> dict:
        """RPC state_push 用 snapshot。

        字段集 (见 doc/RPC.md §5.2 ApproachSensor 表):
        - ``module_id``          host mesh 名
        - ``kind``               ``"approach_sensor"``
        - ``name``               host mesh 名 (同 module_id)
        - ``state``              runtime state
        - ``is_triggered``       bool, 本 tick 扫描结果
        - ``triggered_obj_name`` str, 第一个 hit 的对象名 (空 = 无命中)
        - ``cube_size``          (x, y, z), 感应盒长宽高
        - ``working_face_center`` (x, y, z), 感应盒中心偏移
        - ``stop_reason``        当前恒为空字符串 (本 module 没有 stop_reason 概念)
        """
        # 实时读 cfg, 让 snapshot 反映 artist 在面板上调整 cube_size 等。
        cube_size: Tuple[float, float, float] = (1.0, 1.0, 1.0)
        face_center: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        try:
            sensor = ApproachSensor(self.host_obj, trigger_obj=None)
            cube_size = tuple(sensor.cube_size)
            face_center = tuple(sensor.working_face_center)
        except Exception:
            pass
        return {
            "module_id": self.module_id,
            "kind": self.kind,
            "name": self.module_id,
            "state": self.state,
            "is_triggered": bool(self._is_triggered),
            "triggered_obj_name": self._triggered_obj_name or "",
            "cube_size": cube_size,
            "working_face_center": face_center,
            "stop_reason": "",
        }


# ---- 离线 / 测试用的 Vector 替身 (与 approach_sensor 同款) ----------


def _Vec(t):
    """构造一个支持 ``@`` 矩阵乘法 + ``[i]`` 索引的最小 Vector。

    优先使用 Blender 的 ``mathutils.Vector``, 离线测试 fallback 到这个替身。
    """
    try:
        from mathutils import Vector as _BlenderVector
        return _BlenderVector((float(t[0]), float(t[1]), float(t[2])))
    except ImportError:
        return _FakeVec(t)


class _FakeVec:
    __slots__ = ("x", "y", "z")

    def __init__(self, t):
        self.x = float(t[0])
        self.y = float(t[1])
        self.z = float(t[2])

    def __getitem__(self, i):
        return (self.x, self.y, self.z)[i]

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z

    def __repr__(self):
        return f"_FakeVec({self.x}, {self.y}, {self.z})"


__all__ = [
    "ApproachSensorModule",
    "STATE_DISABLED",
    "STATE_ACTIVE",
    "STATE_BLOCKED",
    "ALL_STATES",
    "VALID_ACTIONS",
    "BLOCKED_ALLOWED_ACTIONS",
]