# -*- coding: utf-8 -*-
"""ConveyorModule:综合对象 conveyor 的运行时。

设计概述
--------
``ConveyorModule`` 是 :class:`BaseSimulationModule` 的子类,
``kind="conveyor"``,``category="axes"``。
它**完全独立**实现:不动 host / roller / belt,只**位置推动**场景里
其它物体(被皮带接触的刚体):每 tick 沿 belt_dir 写 location 增量(纯位置
计算,不依赖 Bullet/摩擦,不碰 frame 逻辑),并往 belt mesh 写
``belt_uv_offset`` 自定义属性供材质 Mapping 节点用。

关键不变式
----------
1. **方向 ``belt_dir``** = ``(idler.world_pos - drive.world_pos).normalized()``
   默认 forward 语义是从 drive 推向 idler(即 ``+belt_dir`` 方向)。
   两 roller 世界位置重合 → discovery 报错。
2. **状态机**只有 4 个状态;``running`` toggle 控制 IDLE / RUNNING 切换,
   collision 全局 marker 控制 BLOCKED 进入 / 退出。
3. **每帧在 belt_dir 方向位置推动被驱动物体**(写 location,不积分、
   不靠 Bullet/摩擦)。推动前做**刚体阻挡预判(1a)**:位移会侵入其它刚体
   时,收缩到刚好停在接触处;下个 tick 继续尝试,对面让开后自动续走。
   Stop 后下一帧 fallthrough 不写即可。
4. **不写 angular_velocity / mass / rigid_body 其它字段**(那些是物理
   仿真参数,artist 自己设);没装 RigidBody 的物体直接跳过。

状态机
------
::

    IDLE(running=False) ──set_running(True)──> RUNNING
    RUNNING ──set_running(False) / stop──> IDLE
    * ──collision engine hit / marker 置位──> BLOCKED
    BLOCKED ──reset_collision / marker 清除──> IDLE

外部信号映射
------------
- ``set_running(bool)`` —— 启停控制
- ``set_speed(float)`` —— 速度(>= 0)
- ``set_direction(int)`` —— 方向(+1 forward / -1 reverse)
- ``set_friction(float)`` —— 抓地系数(0..1)
- ``stop()`` / ``idle()`` —— 等价 set_running(False)
- ``reset_collision()`` —— 清 scene marker 并解除 BLOCKED

collision
---------
本模块与 :class:`LinearAxis` / :class:`CylinderModule` 共用**同一套**
collision 契约(同一个单例 :class:`CollisionEngine`):

1. **不检查自身碰撞**: ``bodies = ()`` —— conveyor 不产自身运动,自身不
   参与碰撞检测(同 VacuumNozzle 风格)。
2. **全局停车**: 命中且引擎启用时进 ``BLOCKED`` + ``stop_reason="collision"``,
   并 ``engine.mark_collision()`` 写 scene 级 marker。其它模块下一 tick
   读到该 marker 也会进 BLOCKED —— 即"任一模块碰撞、全体停车"。
3. **只认 reset_collision**: marker 置位期间本模块不推进驱动逻辑,
   且除 ``reset_collision`` 之外的运动相关命令全部被拒(``set_running`` /
   ``set_speed`` / ``set_direction`` / ``set_friction`` 状态位类允许写入,
   ``stop`` / ``idle`` 会改 ``state``,必须拒绝)。
4. **自动解锁**: 别的模块调 ``reset_collision`` 清掉 marker 后,本模块
   下一 tick 自行退出 BLOCKED。
5. **引擎禁用 = kill switch**: ``engine.is_enabled`` 为 False 时仍调
   ``update``(供 Dev panel 显示状态),但**不会**把本模块锁进 BLOCKED;
   同时 :meth:`CollisionEngine.set_disabled` 会主动调本模块的
   :meth:`_clear_blocked_state_only` 立即释放已锁定的模块。
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

try:
    from ...framework import BaseSimulationModule, SimulationCommand, is_object_alive
    from ...modules.components.collision import STOP_REASON_COLLISION
except ImportError:  # pragma: no cover —— 离线/直接脚本 import 路径
    from framework import (  # noqa: F401
        BaseSimulationModule,
        SimulationCommand,
        is_object_alive,
    )
    from modules.components.collision import STOP_REASON_COLLISION  # noqa: F401

# NOTE: 与 component.py 解耦 —— runtime 不 import component (component
# 会 import bpy,在离线 pytest 里没有 bpy)。DIR_* 常量在 runtime 自身
# 重新定义并保持与 component 一致。
DIR_FORWARD = 1
DIR_REVERSE = -1


# ---- 状态常量 ----

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_BLOCKED = "blocked"

ALL_STATES = (
    STATE_IDLE,
    STATE_RUNNING,
    STATE_BLOCKED,
)

VALID_ACTIONS = (
    "set_running",
    "set_speed",
    "set_direction",
    "set_friction",
    "start",
    "stop",
    "idle",
    "reset_collision",
)

STOP_REASON_NONE = ""
STOP_REASON_RUNNING = "running"
STOP_REASON_CMD_STOP = "cmd_stop"
STOP_REASON_CMD_IDLE = "cmd_idle"
STOP_REASON_INVALID_DIR = "invalid_direction"

# 供外部读取的"BLOCKED 时可用哪些动作"清单。与 LinearAxis / Cylinder 同构:
# 状态位类(set_running / set_speed / set_direction / set_friction /
# reset_collision)允许写入;``stop`` / ``idle`` 会改 ``state``,必须拒绝。
# ``start`` ≡ set_conveyor_running(running=true, ...),本质也是状态位类
# 写入(set_running 在 BLOCKED 期间只写信号位不改 state,见其 docstring),
# 因此与 set_running 同等待遇 —— BLOCKED 期间允许,解锁后生效。
BLOCKED_ALLOWED_ACTIONS = (
    "set_running",
    "set_speed",
    "set_direction",
    "set_friction",
    "start",
    "reset_collision",
)

# 单位换算:1 BU = 1 mm 时换算系数(同 LinearAxis / Cylinder)
MM_TO_BU = 0.001

# 接触检测默认容差
DEFAULT_HEIGHT_TOLERANCE_BU = 5     # 高度容差 5cm
DEFAULT_LATERAL_TOLERANCE_BU = 2    # 横向容差 2cm

# 1a 位置推动"顶住即停"的接触容差:某轴侵入深度 <= 容差视为接触(不阻挡),
# 超过才视为阻挡(贴合支撑面、侧向轻擦不会误冻结被驱动物体)。
DEFAULT_PUSH_TOLERANCE_BU = 0.02
# 位移比例二分收敛次数(8 次 → 1/256 精度,足够"停在接触处")
PUSH_BISECT_STEPS = 8

# uv_offset 增量缩放(纹理按 belt 长度归一化滚动)
DEFAULT_UV_SCALE = 1.0

# belt mesh 上写入的自定义属性 key(材质 Mapping 节点 driver 接它)
BELT_UV_OFFSET_KEY = "belt_uv_offset"

# ---- helpers ----------------------------------------------------------


def _obj_world_pos(obj) -> Optional[Tuple[float, float, float]]:
    """返回 obj 的 world 位置 ``(x, y, z)``。退化时返回 None。"""
    if obj is None:
        return None
    try:
        # 优先走 matrix_world.translation(在线版本稳定)
        try:
            t = obj.matrix_world.translation
            return (float(t[0]), float(t[1]), float(t[2]))
        except Exception:
            pass
        loc = obj.location
        return (float(loc[0]), float(loc[1]), float(loc[2]))
    except Exception:
        return None


def _vec_sub(a, b):
    """3D 向量减法, ``a - b``"""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


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
    """scale 计算, ``v * s``"""
    return (v[0] * s, v[1] * s, v[2] * s)


def _obj_world_aabb(obj):
    """取 obj 在世界空间的 8 角点 AABB(列表)或返回 None(失败时)。

    优先用 :attr:`bound_box`(8 个 local 角点);无则退回 :attr:`dimensions`
    + :attr:`location`(以 center 推 AABB)。Offline mock 也能用(只要有
    bound_box)。
    """
    if obj is None:
        return None
    try:
        bb = getattr(obj, "bound_box", None)
        if bb is not None:
            try:
                mw = obj.matrix_world
            except Exception:
                mw = None
            corners = []
            for c in bb:
                try:
                    pt = (float(c[0]), float(c[1]), float(c[2]))
                except Exception:
                    continue
                if mw is not None:
                    try:
                        pt = mw @ pt
                    except Exception:
                        try:
                            t = mw.translation
                            pt = (pt[0] + t[0], pt[1] + t[1], pt[2] + t[2])
                        except Exception:
                            pass
                corners.append(pt)
            if len(corners) >= 8:
                return corners[:8]
        # fallback: 用 location + dimensions 推一个简化 8 角点 AABB
        loc = _obj_world_pos(obj)
        dims = getattr(obj, "dimensions", None)
        if loc is not None and dims is not None:
            try:
                dx = float(dims[0]) * 0.5
                dy = float(dims[1]) * 0.5
                dz = float(dims[2]) * 0.5
            except Exception:
                dx = dy = dz = 0.0
            x0, y0, z0 = loc[0] - dx, loc[1] - dy, loc[2] - dz
            x1, y1, z1 = loc[0] + dx, loc[1] + dy, loc[2] + dz
            return [
                (x0, y0, z0), (x1, y0, z0),
                (x0, y1, z0), (x1, y1, z0),
                (x0, y0, z1), (x1, y0, z1),
                (x0, y1, z1), (x1, y1, z1),
            ]
    except Exception:
        pass
    return None


def _obj_world_vbox(obj):
    """按**真实网格顶点**算世界 AABB(8 角点),无顶点数据时回退
    :func:`_obj_world_aabb`。

    ``bound_box`` 在部分对象上与真实网格不一致(线上实测 TestBoard 的
    bound_box 与 ``data.vertices`` 尺寸差出数倍),1a 阻挡判定必须用真实
    顶点,否则会把"没碰到"误判成"已顶死"而永久卡住。离线 mock 无
    ``data.vertices``,自动走 bound_box 回退,行为不变。
    """
    verts = _iter_world_vertices(obj)
    if verts:
        mn = [min(v[i] for v in verts) for i in range(3)]
        mx = [max(v[i] for v in verts) for i in range(3)]
        return [
            (mn[0], mn[1], mn[2]), (mx[0], mn[1], mn[2]),
            (mn[0], mx[1], mn[2]), (mx[0], mx[1], mn[2]),
            (mn[0], mn[1], mx[2]), (mx[0], mn[1], mx[2]),
            (mn[0], mx[1], mx[2]), (mx[0], mx[1], mx[2]),
        ]
    return _obj_world_aabb(obj)


def _aabb_min_max(corners):
    """8 角点 AABB → (min_xyz, max_xyz) 各 3 元组。"""
    if not corners:
        return None, None
    mn = list(corners[0])
    mx = list(corners[0])
    for c in corners[1:]:
        for i in range(3):
            if c[i] < mn[i]:
                mn[i] = c[i]
            if c[i] > mx[i]:
                mx[i] = c[i]
    return tuple(mn), tuple(mx)


def _get_context_scene():
    """取当前场景;离线/无 bpy 时返回 None。"""
    try:
        import bpy
        return bpy.context.scene
    except Exception:
        return None


# ---- _scan_objects_on_belt 的独立辅助函数(模块级) --------------------


def _compute_belt_contact_zone(belt_mesh, belt_dir):
    """返回 ``(belt_top_z, belt_bot_z, belt_center, proj_min, proj_max)``。

    belt mesh 缺失或 AABB 退化时返回 ``None``。belt_dir 是 Conveyor
    模块构造期推导出的世界空间归一化方向。
    """
    belt_corners = _obj_world_aabb(belt_mesh)
    if not belt_corners:
        return None
    belt_min, belt_max = _aabb_min_max(belt_corners)
    if belt_min is None or belt_max is None:
        return None
    belt_center = (
        0.5 * (belt_min[0] + belt_max[0]),
        0.5 * (belt_min[1] + belt_max[1]),
        0.5 * (belt_min[2] + belt_max[2]),
    )
    proj_min = proj_max = None
    for c in belt_corners:
        rel = _vec_sub(c, belt_center)
        proj = _vec_dot(rel, belt_dir)
        if proj_min is None or proj < proj_min:
            proj_min = proj
        if proj_max is None or proj > proj_max:
            proj_max = proj
    if proj_min is None or proj_max is None:
        return None
    return belt_max[2], belt_min[2], belt_center, proj_min, proj_max


def _collect_free_recursive(obj, skip_set, candidates):
    """递归收集“独立 mesh + rigid_body”对象，遇 skip 集合里节点整棵停止。

    :param obj: 当前节点
    :param skip_set: “已认领” host 集合（见
        :meth:`ConveyorModule._collect_skip_set`）
    :param candidates: 输出列表（符合 ``MESH + rigid_body`` 的对象追加进去）
    """
    if obj is None or obj in skip_set:
        return
    if not is_object_alive(obj):
        return
    if obj.type == "MESH" and getattr(obj, "rigid_body", None) is not None:
        candidates.append(obj)
    for child in getattr(obj, "children", ()) or ():
        _collect_free_recursive(child, skip_set, candidates)


def _iter_world_vertices(obj):
    """迭代 obj 的世界空间顶点坐标列表。

    优先走 ``obj.data.vertices``（真实 Blender mesh），失败/无 ``data``
    时退回 ``bound_box`` 8 角点（测试用 mock 也走这条）。返回值是
    ``[(x, y, z), ...]``；失败返回 ``None``。
    """
    # 路径 1: 真实 mesh vertices
    try:
        mesh = getattr(obj, "data", None)
        if mesh is not None and hasattr(mesh, "vertices") and len(mesh.vertices) > 0:
            try:
                mw = obj.matrix_world
            except Exception:
                return None
            out = []
            for v in mesh.vertices:
                try:
                    co = v.co
                    out.append((
                        mw[0][0] * co[0] + mw[0][1] * co[1] + mw[0][2] * co[2] + mw[0][3],
                        mw[1][0] * co[0] + mw[1][1] * co[1] + mw[1][2] * co[2] + mw[1][3],
                        mw[2][0] * co[0] + mw[2][1] * co[1] + mw[2][2] * co[2] + mw[2][3],
                    ))
                except Exception:
                    continue
            return out
    except Exception:
        pass
    # 路径 2: 退回 AABB 8 角点（mock / mesh 缺失 fallback）
    return _obj_world_aabb(obj)


# ---- 主类 -----------------------------------------------------------


class ConveyorModule(BaseSimulationModule):
    """Conveyor 运行时模块。"""

    __slots__ = (
        "host_obj",
        "drive_roller",
        "idler_roller",
        "belt",
        "_collision_engine",
        "_scene",
        "state",
        "_running",
        "_target_speed",
        "_direction_sign",
        "_friction",
        "_belt_dir",
        "_belt_length",
        "_uv_offset",
        "_driven_objs",
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
        drive_roller,
        idler_roller,
        belt,
        collision_engine=None,
        module_id: Optional[str] = None,
        scene=None,
    ):
        mid = module_id if module_id is not None else host_obj.name
        super().__init__(module_id=mid, kind="conveyor", category="axes")
        self.host_obj = host_obj
        self.drive_roller = drive_roller
        self.idler_roller = idler_roller
        self.belt = belt
        self._collision_engine = collision_engine
        # 缓存构造期的 scene,后续 update() 直接用(不依赖 bpy.context,
        # 哪天 update() 在 timer 线程跑也能命中)
        self._scene = scene if scene is not None else _get_context_scene()

        # ---- 推导 belt_dir + belt_length ----
        # 默认 forward = drive 推向 idler,即 +belt_dir 方向。
        drive_pos = _obj_world_pos(drive_roller)
        idler_pos = _obj_world_pos(idler_roller)
        if drive_pos is None or idler_pos is None:
            raise ValueError(
                f"[Conveyor] {mid}: cannot read world_pos of drive/idler roller"
            )
        delta = _vec_sub(idler_pos, drive_pos)
        length = _vec_length(delta)
        if length <= 1e-9:
            raise ValueError(
                f"[Conveyor] {mid}: drive_roller and idler_roller have "
                f"identical world positions; cannot derive belt_dir"
            )
        self._belt_dir: Tuple[float, float, float] = _vec_normalize(delta)
        self._belt_length: float = length

        # ---- 初始 cfg 镜像 ----
        self._target_speed: float = 50.0
        self._direction_sign: int = DIR_FORWARD
        self._friction: float = 1.0
        self._running: bool = False
        self._sync_from_cfg()

        # ---- 运行时状态 ----
        self.state: str = STATE_RUNNING if self._running else STATE_IDLE
        self._stop_reason: str = (
            STOP_REASON_RUNNING if self._running else STOP_REASON_NONE
        )
        # uv_offset 累加器(永远累加,不因为 Start/Stop 重置 —— 视觉上
        # 看到的是"连续旋转"的纹理滚动)
        self._uv_offset: float = 0.0
        # 本 tick 扫描到的"被驱动物体"集合(用于 snapshot)
        self._driven_objs: List = []
        self._alive: bool = True
        # 1a/5.x 驱动辅助:被驱动期间持有 kinematic 的对象 {name: (obj, 原值)}
        self._drive_kinematic: dict = {}

    # ---- 内部 cfg 同步 ----

    def _sync_from_cfg(self) -> None:
        """每 tick 把 host.conveyor.{target_speed, direction, friction, running}
        镜像到 instance。让 panel toggle 与 set_* 命令两条路径都生效。
        """
        cfg = getattr(self.host_obj, "conveyor", None)
        if cfg is None:
            return
        try:
            ts = float(getattr(cfg, "target_speed", 50.0))
            self._target_speed = max(0.0, ts)
        except Exception:
            pass
        try:
            d = getattr(cfg, "direction", "forward")
            self._direction_sign = DIR_FORWARD if d == "forward" else DIR_REVERSE
        except Exception:
            pass
        try:
            f = float(getattr(cfg, "friction", 1.0))
            # 截断到 [0, 1]
            self._friction = max(0.0, min(1.0, f))
        except Exception:
            pass
        try:
            self._running = bool(getattr(cfg, "running", False))
        except Exception:
            pass

    def _sync_running_to_cfg(self) -> None:
        """instance → cfg 写回(running 字段单独写,因为 _sync_from_cfg
        只读不写,避免覆盖外部刚 toggle 的值;这里只写 running 一项)。
        """
        cfg = getattr(self.host_obj, "conveyor", None)
        if cfg is None:
            return
        try:
            cfg.running = self._running
        except Exception:
            pass

    # ---- public 命令接口 ----

    def set_running(self, running: bool) -> None:
        """启停控制。True = 进入 STATE_RUNNING;False = STATE_IDLE。

        set_running(False) 即使在 BLOCKED 期间也会执行 —— 状态位是
        "状态控制信号",允许改。
        """
        self._running = bool(running)
        self._sync_running_to_cfg()
        # 不在 BLOCKED 时切 state。BLOCKED 时不强制改 state,等 marker 清
        # 掉后 _clear_blocked_state_only() 会根据 running 决定回到 IDLE 还是
        # RUNNING。
        if self.state != STATE_BLOCKED:
            if self._running:
                self.state = STATE_RUNNING
                self._stop_reason = STOP_REASON_RUNNING
            else:
                self.state = STATE_IDLE
                self._stop_reason = STOP_REASON_NONE

    def set_speed(self, speed: float) -> None:
        """设置目标速度(>= 0)。"""
        try:
            s = float(speed)
        except Exception:
            s = 0.0
        self._target_speed = max(0.0, s)
        cfg = getattr(self.host_obj, "conveyor", None)
        if cfg is not None:
            try:
                cfg.target_speed = self._target_speed
            except Exception:
                pass

    def set_direction(self, direction: int) -> None:
        """设置传送方向。``+1`` = forward, ``-1`` = reverse。"""
        try:
            d = int(direction)
        except Exception:
            d = DIR_FORWARD
        # 校验 +1 / -1,其它值兜底为 forward
        if d not in (DIR_FORWARD, DIR_REVERSE):
            self._stop_reason = STOP_REASON_INVALID_DIR
            d = DIR_FORWARD
        else:
            # 切方向不是错误,清掉之前的 invalid_dir 标记
            if self._stop_reason == STOP_REASON_INVALID_DIR:
                self._stop_reason = (
                    STOP_REASON_RUNNING if self._running else STOP_REASON_NONE
                )
        self._direction_sign = d
        cfg = getattr(self.host_obj, "conveyor", None)
        if cfg is not None:
            try:
                cfg.direction = "forward" if d == DIR_FORWARD else "reverse"
            except Exception:
                pass

    def set_friction(self, friction: float) -> None:
        """设置抓地系数 0..1(越界截断)。"""
        try:
            f = float(friction)
        except Exception:
            f = 1.0
        self._friction = max(0.0, min(1.0, f))
        cfg = getattr(self.host_obj, "conveyor", None)
        if cfg is not None:
            try:
                cfg.friction = self._friction
            except Exception:
                pass

    def stop(self) -> None:
        """stop = set_running(False)。"""
        self.set_running(False)
        self._stop_reason = STOP_REASON_CMD_STOP

    def idle(self) -> None:
        """idle = set_running(False)。"""
        self.set_running(False)
        self._stop_reason = STOP_REASON_CMD_IDLE

    # ---- collision 阻塞路径(契约对齐 LinearAxis / Cylinder / VacuumNozzle)----

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块命中 → 进 BLOCKED(集中式拉模式回调)。"""
        self._enter_blocked_state()

    def on_collision_cleared(self) -> None:
        """碰撞解除 / 引擎被禁用 → 退出 BLOCKED(集中式拉模式回调)。"""
        self._clear_blocked_state_only()

    def _enter_blocked_state(self) -> None:
        """把本模块锁进 BLOCKED(纯本地状态变更)。

        注意:**本方法不写 scene marker**。marker 由"发现碰撞的那一方"
        写 —— 集中式拉模式下是 :meth:`CollisionEngine.step`,
        兼容路径下是 :func:`collision.module_self_check`;marker 分支
        (别的模块撞了 → 本模块跟着停)进入 BLOCKED 时更不该重复写。
        """
        if self.state == STATE_BLOCKED:
            return
        self.state = STATE_BLOCKED
        self._stop_reason = STOP_REASON_COLLISION

    def _clear_blocked_state_only(self) -> None:
        """本地退出 BLOCKED,不触碰 engine 的 scene marker。

        由 :class:`CollisionEngine.set_disabled` 按方法名遍历调用;
        命名 ``_clear_blocked_state_only`` 不可改。
        """
        if self.state != STATE_BLOCKED:
            return
        if self._running:
            self.state = STATE_RUNNING
            self._stop_reason = STOP_REASON_RUNNING
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
        # BLOCKED 门控(与 LinearAxis / Cylinder / VacuumNozzle 同构)
        if (self.state == STATE_BLOCKED
                and action not in BLOCKED_ALLOWED_ACTIONS):
            return
        if action == "set_running":
            self.set_running(bool(payload.get("running", self._running)))
        elif action == "set_speed":
            self.set_speed(float(payload.get("target_speed", self._target_speed)))
        elif action == "set_direction":
            self.set_direction(int(payload.get("direction", self._direction_sign)))
        elif action == "set_friction":
            self.set_friction(float(payload.get("friction", self._friction)))
        elif action == "start":
            # start ≡ 顶层 RPC ``set_conveyor_running(running=true, ...)``:
            # payload 字段与该 RPC 的 params 同名同语义,全部可选
            # (缺省 = 不改),调用顺序也一致(set_running → set_speed →
            # set_direction → set_friction;setter 互不依赖,顺序只影响
            # stop_reason:非法方向的 invalid_direction 标记不会被后续
            # set_running 的 running 标记覆盖 —— 与 set_conveyor_running
            # 的行为完全一致)。
            #
            # 唯一差异:``running`` 缺省为 True —— 这正是 "start" 的语义
            # (set_conveyor_running 里 running 缺省 = 不变)。
            #
            #   {"target_speed": 50.0, "direction": 1, "friction": 1.0}
            #
            # 类型/范围宽容度与 set_* action 一致(与专用 RPC 的严格
            # INVALID_PARAMS 校验不同):set_speed 内部 max(0, ..) 夹紧,
            # set_direction 对非法值兜底 forward。
            self.set_running(bool(payload.get("running", True)))
            if "target_speed" in payload:
                self.set_speed(float(payload["target_speed"]))
            if "direction" in payload:
                self.set_direction(int(payload["direction"]))
            if "friction" in payload:
                self.set_friction(float(payload["friction"]))
        elif action == "reset_collision":
            self.reset_collision()
        elif action == "stop":
            self.stop()
        elif action == "idle":
            self.idle()

    # ---- reset / snapshot ----

    def reset(self) -> None:
        """reset 接口:停下运行、清空 uv_offset、回到 IDLE。"""
        self.set_running(False)
        self._uv_offset = 0.0
        self._stop_reason = STOP_REASON_NONE

    def snapshot(self) -> dict:
        """RPC state_push 用 snapshot。

        字段集(为控制 state_push 带宽,只保留控制面/状态面字段;
        几何/滚动类的 ``belt_dir`` / ``belt_speed_world`` /
        ``uv_offset`` / ``driven_count`` 已移出推流,UI 面板直接读
        实例内部值,见 ``Conveyor ui.py`` / ``dev_panel.py``):

        - ``module_id``        模块 id
        - ``kind`` / ``name``  kind + module_id(与其它 module 同构)
        - ``state``            runtime state
        - ``running``          镜像 cfg(瞬时;判断启停以此为准)
        - ``direction``        ``+1`` forward / ``-1`` reverse
        - ``target_speed``     线速度(数值直接当 BU/s 用)
        - ``friction``         0..1
        - ``driven_names``     本 tick 被驱动物体名列表(数量 = len)
        - ``stop_reason``      停止原因

        不暴露 ``_belt_length`` / ``_direction_sign``(可在 direction +
        target_speed 推得);也不暴露 ``_alive``(stale 哨兵)。
        """
        # 现场算当前 v_belt(可能 state == BLOCKED 时也算,只是不写物体)
        # —— 不再进 snapshot(带宽考虑),仅本地用途保留。
        return {
            "module_id": self.module_id,
            "kind": self.kind,
            "name": self.module_id,
            "state": self.state,
            "running": bool(self._running),
            "direction": int(self._direction_sign),
            "target_speed": float(self._target_speed),
            "friction": float(self._friction),
            "driven_names": [
                getattr(o, "name", None) for o in self._driven_objs
            ],
            "stop_reason": self._stop_reason,
        }

    # ---- collision structure declaration ----

    def collision_structure(self):
        """声明 Conveyor 的碰撞结构(见 :class:`CollisionStructure`)。

        - **members**:host + drive_roller + idler_roller + belt。
          三者同属一组,组内永不互撞(同 Cylinder / LinearAxis 风格)。
        - **bodies**:空 —— conveyor 不产生自身运动,只驱动场景里
          **其它**物体的 rigid_body.linear_velocity(那些物体不是
          conveyor 的部件,由 engine 自动归到 ``structure:<根名>`` 组合)。

        被驱动的物体(放 cube 在皮带上)不进 members:它们不在 conveyor 的
        保护伞下,被其它模块(LinearAxis slider 等)撞到仍然算真碰撞,
        触发全局 BLOCKED。这是 design choice —— "声明即永久免疫"的旧版
        行为在 VacuumNozzle 已被移除。
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
        for field in ("drive_roller", "idler_roller", "belt"):
            members.append(unwrap_object(getattr(self, field, None)))
        return CollisionStructure(
            module_id=self.module_id,
            host=self.host_obj,
            members=tuple(m for m in members if m is not None),
            bodies=(),
        )

    # ---- 内部辅助 ----

    def _compute_world_belt_speed(self) -> Tuple[float, float, float]:
        """当前帧的 belt 速度向量(BU/s)。不写物体,纯查询。

        target_speed 直接当 BU/s 使用,不再做 ``* MM_TO_BU`` 换算
        (artist 在 UI 上调的数值就是 BU/s 语义)。
        """
        return _vec_scale(
            self._belt_dir, self._direction_sign * self._target_speed
        )

    # ---- _scan_objects_on_belt 辅助 ----

    def _collect_skip_set(self):
        """返回应跳过的“host 集合”（其它 module 的 host + 自己 4 部件）。

        任何一个 root 节点在 skip 集合中，其整棵 subtree 会被
        :func:`_collect_free_recursive` 跳过——避免推动机器本身的部件，
        也避免另一个 module 把自家 subtree 中的 RigidBody 误推。
        """
        skip = set()
        # 其它已注册 module 的 host
        try:
            from ...addon import get_manager
            mgr = get_manager()
        except Exception:
            mgr = None
        if mgr is not None:
            try:
                for m in mgr.all():
                    if m is self:
                        continue
                    host = getattr(m, "host_obj", None)
                    if host is not None:
                        skip.add(host)
            except Exception:
                pass
        # 自己 conveyor 的 4 部件
        for attr in ("host_obj", "belt", "drive_roller", "idler_roller"):
            obj = getattr(self, attr, None)
            if obj is not None:
                skip.add(obj)
        skip.discard(None)
        return skip

    def _has_face_contact(
        self,
        obj,
        belt_center,
        belt_bot_z,
        belt_top_z,
        proj_min,
        proj_max,
        h_tol,
    ) -> bool:
        """obj 是否有一个面与 belt 表面接触。

        “面接触”近似为：任一顶点的 Z 落在
        ``[belt_bot - h_tol, belt_top + h_tol]`` 范围内、且该顶点在
        ``belt_dir`` 上的投影落在 ``[proj_min, proj_max]`` 区间内。
        对典型刚体（立方体 / 圆柱体），这等价于“底面与 belt 顶面接触”。
        """
        z_lo = belt_bot_z - h_tol
        z_hi = belt_top_z + h_tol
        verts = _iter_world_vertices(obj)
        if verts is None:
            return False
        cx, cy, cz = belt_center
        bx, by, bz = self._belt_dir
        for v in verts:
            if z_lo <= v[2] <= z_hi:
                proj = (v[0] - cx) * bx + (v[1] - cy) * by + (v[2] - cz) * bz
                if proj_min <= proj <= proj_max:
                    return True
        return False

    def _scan_objects_on_belt(self) -> List:
        """扫描场景里接触 belt 的“独立刚体”物体集合。

        简化后的判定逻辑（按“独立 mesh + 有刚体 + 有面接触”三重过滤）：

        1. **场景树遍历**：从顶层 root 出发递归（不走 flat 列表）。若某节点
           是 “已认领的 host”（自己的 host_obj / belt / 两 roller，或
           其它已注册 module 的 host_obj），**整棵 subtree 跳过**——避免
           推动 LinearAxis slider / VacuumNozzle 内部物体这种隐式联锁。
        2. **过滤**：被遍历到的对象必须是 ``type == "MESH"`` 且装上
           ``rigid_body``；LinearAxis1 / Cylinder1 / VacuumNozzle1 等
           EMPTY host（即使有 rigid_body）因为 type 不匹配也被跳过。
        3. **面接触**：对象的任一顶点 Z 落在
           ``[belt_bot - h_tol, belt_top + h_tol]`` 范围内、且该顶点在
           ``belt_dir`` 上的投影落在 ``[proj_min, proj_max]`` 区间内 →
           即认为该物体有一个面与 belt 表面接触。

        返回值写入 :attr:`_driven_objs`。
        """
        scene = self._scene if self._scene is not None else _get_context_scene()
        if scene is None:
            return []

        # 1. belt 接触区参数（沿 belt_dir 投影范围 + Z 范围）。
        zone = _compute_belt_contact_zone(self.belt, self._belt_dir)
        if zone is None:
            return []
        belt_top_z, belt_bot_z, belt_center, proj_min, proj_max = zone

        # 2. “已认领”的 host 集合（其它 module + 自己 4 部件）。
        skip_set = self._collect_skip_set()
        if not skip_set:
            return []  # 自己模块连 host/belt/roller 都没配对，不会驱动任何东西

        # 3. 递归遍历场景树，收集“独立 mesh + rigid_body”候选。
        candidates: List = []
        for root in getattr(scene, "objects", ()) or ():
            # 只从顶层 root 出发：非 root 会被某个 parent 的递归覆盖到。
            if getattr(root, "parent", None) is not None:
                continue
            _collect_free_recursive(root, skip_set, candidates)

        # 4. 对候选逐一做面接触判定。
        h_tol = DEFAULT_HEIGHT_TOLERANCE_BU
        out: List = []
        for obj in candidates:
            if self._has_face_contact(
                obj, belt_center, belt_bot_z, belt_top_z,
                proj_min, proj_max, h_tol,
            ):
                out.append(obj)
        return out


    # ---- 1a 刚体阻挡预判(位置推动"顶住即停") ----

    def _same_unit_names(self, obj) -> set:
        """obj 所属"同一刚体单元"的对象名集合。

        包含:obj 自身 + 祖先链 + 全部后代(父子是一起动的整体),
        以及 conveyor host 的整棵子树(皮带/滚筒是支撑与传动部件,不算
        障碍;场景里可能有多条皮带方块,如 Belt_Top + Belt_Top.002)。
        """
        names = set()

        def _add(o):
            n = getattr(o, "name", None)
            if n is not None:
                names.add(n)

        def _desc(o):
            for c in getattr(o, "children", ()) or ():
                _add(c)
                _desc(c)

        cur = obj
        while cur is not None:
            _add(cur)
            cur = getattr(cur, "parent", None)
        _desc(obj)
        host = getattr(self, "host_obj", None)
        if host is not None:
            _add(host)
            _desc(host)
        return names

    def _rigid_blocker_boxes(self, obj) -> List:
        """收集阻挡候选 ``[(min_xyz, max_xyz), ...]``。

        刚体识别口径与被驱动对象一致(``type == "MESH"`` 且带
        ``rigid_body``)—— 这就是"刚体 vs 刚体"的 1a 语义:只有刚体之间
        的顶撞走阻挡收缩处理。
        """
        scene = self._scene if self._scene is not None else _get_context_scene()
        if scene is None:
            return []
        skip = self._same_unit_names(obj)
        boxes: List = []
        for other in getattr(scene, "objects", ()) or ():
            name = getattr(other, "name", None)
            if name is None or name in skip:
                continue
            if getattr(other, "type", None) != "MESH":
                continue
            if getattr(other, "rigid_body", None) is None:
                continue
            if not is_object_alive(other):
                continue
            mn, mx = _aabb_min_max(_obj_world_vbox(other))
            if mn is None:
                continue
            boxes.append((mn, mx))
        return boxes

    @staticmethod
    def _aabb_penetrating(mn1, mx1, mn2, mx2, tol) -> bool:
        """两 AABB 是否发生"逐轴侵入深度都超过容差"的重叠(接触/间隙不算)。

        支撑面贴合(轴向侵入≈0)与侧向轻擦都不会被判为阻挡。
        """
        for i in range(3):
            if min(mx1[i], mx2[i]) - max(mn1[i], mn2[i]) <= tol:
                return False
        return True

    def _clamp_delta_by_blockers(self, obj, delta_pos):
        """1a:把推动位移收缩到"刚好不侵入其它刚体"的最大比例。

        全额位移无障碍 → 原样返回;有障碍 → 对位移比例二分
        (:data:`PUSH_BISECT_STEPS` 次)取最大可行比例,实现"顶住停在接触
        处"。每个 tick 都重新尝试全额位移,对面让开后自动续走。
        """
        corners = _obj_world_vbox(obj)
        mn0, mx0 = _aabb_min_max(corners)
        if mn0 is None:
            return delta_pos
        boxes = self._rigid_blocker_boxes(obj)
        if not boxes:
            return delta_pos
        tol = DEFAULT_PUSH_TOLERANCE_BU

        def blocked(f: float) -> bool:
            dx, dy, dz = delta_pos[0] * f, delta_pos[1] * f, delta_pos[2] * f
            mn = (mn0[0] + dx, mn0[1] + dy, mn0[2] + dz)
            mx = (mx0[0] + dx, mx0[1] + dy, mx0[2] + dz)
            for bmn, bmx in boxes:
                if self._aabb_penetrating(mn, mx, bmn, bmx, tol):
                    return True
            return False

        if not blocked(1.0):
            return delta_pos
        lo, hi = 0.0, 1.0
        for _ in range(PUSH_BISECT_STEPS):
            mid = 0.5 * (lo + hi)
            if blocked(mid):
                hi = mid
            else:
                lo = mid
        return (delta_pos[0] * lo, delta_pos[1] * lo, delta_pos[2] * lo)

    def _sync_drive_kinematic(self, driven: List) -> None:
        """被驱动对象在驱动期间置 ``kinematic``,离开时还原原值。

        Blender 5.x:ACTIVE 非 kinematic 刚体的 transform 归 Bullet 所有,
        直接写 ``obj.location`` 会被仿真回写覆盖(线上实测)。置 kinematic
        后写入立即生效;还原 dynamic 时 Bullet 按最近的运动推断速度 ——
        每 tick 一个小位移,推断速度 ≈ 皮带速度,正是"带着速度离开皮带"
        的自然语义。原本就是 kinematic 的对象不还原(本来就 transform 驱动)。
        """
        names = set()
        for obj in driven:
            name = getattr(obj, "name", None)
            if name is not None:
                names.add(name)
            rb = getattr(obj, "rigid_body", None)
            if rb is None or name is None or name in self._drive_kinematic:
                continue
            try:
                self._drive_kinematic[name] = (obj, bool(rb.kinematic))
                rb.kinematic = True
            except Exception:
                pass
        # 不再被驱动的:还原
        for name in list(self._drive_kinematic):
            if name in names:
                continue
            obj, orig = self._drive_kinematic.pop(name)
            try:
                rb = getattr(obj, "rigid_body", None)
                if rb is not None:
                    rb.kinematic = orig
            except Exception:
                pass

    def _apply_velocity(self, obj, v_belt, dt: float) -> None:
        """对一个 obj 推动。按 friction + dt 在 belt_dir 方向位移。

        Blender 5.x 的 :class:`RigidBodyObject` 不再暴露可写的
        ``linear_velocity``,所以这里走 location 直接位移路径(等价于
        "动力学"上的瞬时速度积分):每 tick 在 belt_dir 方向推 Δx =
        v_belt × dt × friction × (v_belt 与物体当前 belt_dir 速度的
        比例补偿)。friction 语义保持:1.0 = 完全抓地, 0.0 = 不动。

        物体物理仿真仍由 Blender 推进;我们的写入被当作"目标位姿"
        —— 没有 active rigid body 时也能工作(无 rigid body 也走这
        条;但 _scan_objects_on_belt 阶段就过滤了无 rigid_body 的物
        体,所以这里假设 obj.rigid_body != None)。
        """
        try:
            rb = getattr(obj, "rigid_body", None)
            if rb is None:
                return
            # 读取物体当前 belt_dir 速度(若可用)。Blender 5.x RigidBodyObject
            # 没有 linear_velocity 属性,这一步优雅失败 → 默认 0。
            cur_v = (0.0, 0.0, 0.0)
            try:
                cur = rb.linear_velocity
                cur_v = (float(cur[0]), float(cur[1]), float(cur[2]))
            except Exception:
                pass
            f = self._friction
            if f <= 0.0:
                # 完全打滑:不动物体
                return
            # 算出沿 belt_dir 的目标速度(物体当前 + 推力)
            cur_axis = _vec_dot(cur_v, self._belt_dir)
            belt_axis = _vec_dot(v_belt, self._belt_dir)
            # friction 语义:1.0 = 完全抓地(物体 v → belt v),
            # 0.0 = 完全打滑(不动)。
            # 沿 belt_dir 一维 lerp
            new_axis_v = cur_axis + (belt_axis - cur_axis) * f
            # 速度写路径(≤4.x)的"速度差"补偿量:写回 cur_v + 差值 = 目标速度
            delta_axis = new_axis_v - cur_axis
            # 位置写路径(5.x):被驱动对象已由 _sync_drive_kinematic 置
            # kinematic,Bullet 不替它移动 → 每 tick 走满 new_axis_v × dt
            delta_pos = (
                self._belt_dir[0] * new_axis_v * dt,
                self._belt_dir[1] * new_axis_v * dt,
                self._belt_dir[2] * new_axis_v * dt,
            )
            # 1a 刚体阻挡预判:顶住就收缩到接触处(每 tick 重试,对面让开续走)
            delta_pos = self._clamp_delta_by_blockers(obj, delta_pos)
            if (
                abs(delta_pos[0]) < 1e-12
                and abs(delta_pos[1]) < 1e-12
                and abs(delta_pos[2]) < 1e-12
            ):
                return
            try:
                # 优先直接写 linear_velocity(Blender 2.7x-4.x 路径);
                # 失败就退到 location 累加。
                try:
                    # 速度写回 = cur_v + belt_dir×速度差 = 目标速度
                    # (delta_pos 现在是"位置增量"口径,不能除 dt 复用)
                    rb.linear_velocity = (
                        cur_v[0] + self._belt_dir[0] * delta_axis,
                        cur_v[1] + self._belt_dir[1] * delta_axis,
                        cur_v[2] + self._belt_dir[2] * delta_axis,
                    )
                except Exception:
                    # Blender 5.x 路径:location 累加。被驱动对象的
                    # kinematic 已由 _sync_drive_kinematic 统一持有
                    # (ACTIVE 刚体的 transform 归 Bullet,只有 kinematic
                    # 状态下写 location 才生效),这里直接写即可。
                    try:
                        loc = obj.location
                        obj.location = (
                            float(loc[0]) + delta_pos[0],
                            float(loc[1]) + delta_pos[1],
                            float(loc[2]) + delta_pos[2],
                        )
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    def _update_belt_uv_offset(self, dt: float) -> None:
        """累加 belt_uv_offset 自定义属性。

        视觉纹理滚动;不受 ``running`` 状态影响(与物理 affordance 解耦)。
        在 RUNNING 时正向滚,在 BLOCKED 阻滞时也按当前 setting 滚 —— 艺术家
        调速度能看到平滑变速,符合"连续旋转"直觉。
        """
        if self.belt is None:
            return
        # 增量 = direction_sign * speed(BU/s) * dt
        # target_speed 直接当 BU/s 读,与 _compute_world_belt_speed 对齐,
        # 不再做 ``* MM_TO_BU`` 换算。
        try:
            speed_bu = float(self._target_speed)
        except Exception:
            speed_bu = 0.0
        # uv_scale 让滚动速率与"皮带表面速率"匹配:每 1 BU 走 uv_scale 个
        # texture unit。artist 可在材质里改。
        uv_scale = DEFAULT_UV_SCALE / max(self._belt_length, 1e-6)
        delta = self._direction_sign * speed_bu * dt * uv_scale
        self._uv_offset += delta
        # 写 belt mesh 自定义属性
        try:
            self.belt[BELT_UV_OFFSET_KEY] = float(self._uv_offset)
        except Exception:
            pass

    # ---- per-tick ----

    def update(self, dt: float) -> None:
        """每个 tick 由 SimulationManager 调用。

        顺序:
            1. 引用存活检查(stale → ``_alive = False``)。
            2. 镜像 cfg → instance。
            3. 全局 collision 门控:marker 已清 → 自动解锁;marker 置位
               → 进 BLOCKED 并放弃本 tick(不写 linear_velocity,只更新
               uv_offset 让视觉不"卡住")。
            4. 本模块自身处于 BLOCKED 时同样短路(同样只更新 uv_offset)。
            5. 若 ``running == False`` → 不扫场景,不写物体。uv_offset 仍
               按当前速度累加(停止时速度=0,uv 不变;但 artist 调过速度后
               即便 running=False 也保留设置)。
            6. 扫场景找接触物体 → 对每个写 v_belt。
            7. uv_offset 累加并写 belt custom property。
        """
        # 1. 引用存活检查
        for ref in (
            self.host_obj, self.drive_roller, self.idler_roller, self.belt,
        ):
            if not is_object_alive(ref):
                self._alive = False
                return

        # 2. 镜像 cfg → instance
        self._sync_from_cfg()

        # 3. 全局 collision 门控(与 LinearAxis / Cylinder / VacuumNozzle 同构)
        if self._collision_engine is not None:
            if (not self._collision_engine.read_marker()
                    and self.state == STATE_BLOCKED):
                self._clear_blocked_state_only()
            elif self._collision_engine.read_marker():
                if self.state != STATE_BLOCKED:
                    self._enter_blocked_state()
                # 仍更新 uv_offset(视觉不卡);释放被驱动对象的 kinematic
                self._sync_drive_kinematic([])
                self._driven_objs = []
                self._update_belt_uv_offset(dt)
                return

        # 3c. 本模块自身处于 BLOCKED 时同样短路
        if self.state == STATE_BLOCKED:
            self._sync_drive_kinematic([])
            self._driven_objs = []
            self._update_belt_uv_offset(dt)
            return

        # 4. running == False → 不扫场景,不写物体
        if not self._running:
            self._sync_drive_kinematic([])
            self._driven_objs = []
            self._update_belt_uv_offset(dt)
            return

        # 5. 扫场景 + 位置推动(1a 阻挡收缩)
        v_belt = self._compute_world_belt_speed()
        # 速度为 0 时也不写物体(friction >= 1 时会把物体速度变 0,没必要)
        if _vec_length(v_belt) <= 1e-9:
            self._sync_drive_kinematic([])
            self._driven_objs = []
            self._update_belt_uv_offset(dt)
            return

        driven = self._scan_objects_on_belt()
        self._driven_objs = driven
        # 先统一持有 kinematic(见 _sync_drive_kinematic),再逐个位置推动
        self._sync_drive_kinematic(driven)
        for obj in driven:
            self._apply_velocity(obj, v_belt, dt)

        # 6. uv_offset 累加
        self._update_belt_uv_offset(dt)


__all__ = [
    "ConveyorModule",
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_BLOCKED",
    "ALL_STATES",
    "VALID_ACTIONS",
    "BLOCKED_ALLOWED_ACTIONS",
    "STOP_REASON_NONE",
    "STOP_REASON_RUNNING",
    "STOP_REASON_CMD_STOP",
    "STOP_REASON_CMD_IDLE",
    "STOP_REASON_INVALID_DIR",
    "BELT_UV_OFFSET_KEY",
    "DEFAULT_HEIGHT_TOLERANCE_BU",
    "DEFAULT_LATERAL_TOLERANCE_BU",
    "DEFAULT_UV_SCALE",
    "MM_TO_BU",
]