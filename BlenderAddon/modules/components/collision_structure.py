# -*- coding: utf-8 -*-
"""碰撞结构声明 / 组关系 / 结构指纹(纯逻辑,不 import bpy)。

本模块只承载三类**纯数据**逻辑,不含任何重叠判定:

1. :class:`CollisionStructure` —— 模块向 :class:`CollisionEngine` 声明
   自己的 3D 结构的契约。声明分两部分:

   - ``members``:属于本结构的对象(host + 静态部件 + 声明的工件)。
     **组内任意两个对象永远不做碰撞检测**(用户需求:同一个 module 是
     "父级",其它都是"子级",集合内部不检测)。
   - ``bodies``:需要被检测的运动体(LinearAxis 的 slider、
     RotateAxis 的 rotator、Cylinder 的 work_bar)。真空吸嘴不产生
     独立运动,``bodies`` 为空。

2. 组关系判定 :func:`groups_are_related` —— 两个模块的 host 在场景树
   上有祖孙关系时,两组整对跳过(物理上挂在一起,例如 Cylinder 挂在
   LinearAxis 的 slider 下、吸嘴挂在 work bar 下)。跨组命中只在两组
   **不相关**时才成立。

3. 结构指纹 :func:`structure_fingerprint` —— 组件 BVH 缓存的失效依据。   指纹覆盖"哪些对象属于哪个组"+"每个对象的父子关系",因此
   **运行时 reparent / 增删对象 / 改名都会立刻改变指纹**,引擎在下一个
   tick 就重建缓存,消除了旧实现固定 30 tick 重建带来的误判窗口。
   不属于任何组的独立障碍物的**位姿**也进指纹(挪动障碍物立即生效);
   运动体与组内成员的位姿**故意不进**指纹 —— 它们每 tick 都在动,否则
   会退化成每 tick 全量重建,那部分由重建节奏兜底。

引擎侧只负责"取结构 → 比指纹 → 建 BVH → 做重叠计算";重叠计算
100% 留在 :mod:`modules.components.collision`。

4. **场景树全量覆盖**:引擎按**场景树**遍历,每条未被子模块认领的根节点都会
   自动打包成一个组合结构(:func:`build_auto_structures`,id 前缀
   ``structure:``),保证**没有任何 mesh 被漏掉**。自动组合与模块声明的组合
   遵守同一条规则 —— **组内不做碰撞检测**;但它 ``host=None``,
   因此不参与“相关组”跳过(静基准件/装饰物与挂在它上面的模块**要**碰)。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, Optional, Set, Tuple


# 旧路径(字段名推断)使用的槽位名单。语义是"任何 module 可能暴露的
# 槽位名",不是"每个 module 都有这些槽位";``getattr`` 缺失即跳过。
#
# 该名单保留两个用途:
# 1. 未实现 ``collision_structure()`` 的模块(含离线测试 mock)走
#    :func:`legacy_structure` 时仍然被正确归类;
# 2. :mod:`simulation_manager` 的 owned-name 缓存(fallback 路径)。
LEGACY_MEMBER_FIELDS: Tuple[str, ...] = (
    "host_obj",
    "slider",
    "rail",
    "rotator",
    "center",
    "shim",
    "work_bar",
    "touch_shim",
    "approach_sensor_1",
    "approach_sensor_2",
    # VacuumNozzle 槽位(全部为**旧版遗留**,新版均已删除):``sensor_mesh``
    # 是旧版的感应区锚点 mesh(新版锚定在 host 自己身上);``trigger_obj``
    # 是旧版“只吸指定对象”的白名单(新版只有自动扫描);``held_obj`` 是旧版的
    # 单数持有槽位(新版 runtime 用 ``_held_objs`` 列表,刻意**不**放进本
    # 名单 —— 否则释放过的工件会被 owned-name 缓存永久拉黑)。
    # 保留这三项仅为兼容旧 ``.blend`` / 旧 mock 的 legacy 推断路径。
    "sensor_mesh",
    "trigger_obj",
    "held_obj",
)

# 旧路径推断运动体时的候选顺序(取第一个非空的)。
LEGACY_BODY_FIELDS: Tuple[str, ...] = ("slider", "rotator", "work_bar")

# 位姿摘要的小数位(6 位足够判断"是否移动过",又不会因浮点噪声抖动)。
_POSE_PRECISION = 6


# ---- 基础访问器(宽泛 duck-typing,离线 mock 也能用) ----


def is_rigid_body_object(obj) -> bool:
    """对象是否是"刚体"(挂了 ``rigid_body``)。duck-typing,离线 mock 亦可。

    2b 豁免规则的识别口径:命中两侧都是刚体(本函数为真)才算"刚体 vs
    刚体"的合法工艺接触,不触发报警;任一侧不是刚体照常报警。
    """
    if obj is None:
        return False
    try:
        return getattr(obj, "rigid_body", None) is not None
    except ReferenceError:
        return False


def object_name(obj) -> Optional[str]:
    """返回对象名;``None`` / 无名字对象返回 ``None``。"""
    if obj is None:
        return None
    try:
        n = getattr(obj, "name", None)
    except ReferenceError:
        return None
    return n if isinstance(n, str) and n else None


def object_parent(obj):
    """返回父对象(取不到返回 ``None``)。"""
    if obj is None:
        return None
    try:
        return getattr(obj, "parent", None)
    except ReferenceError:
        return None


def unwrap_object(holder):
    """把"槽位值"解成真正的 Object。

    模块的槽位有两种形态:直接是 ``bpy.types.Object``,或是组件包装
    (``Slider`` / ``Rail`` / ``Rotator`` / ``ApproachSensor``,它们把
    对象放在 ``.obj`` 上)。与 ``simulation_manager`` 的旧逻辑一致。
    """
    if holder is None:
        return None
    if hasattr(holder, "obj"):
        return getattr(holder, "obj", None)
    return holder


# ---- 声明 ----


@dataclass(frozen=True)
class CollisionStructure:
    """单个模块的碰撞结构声明(模块 → Collision 的契约)。

    ``members`` 里只放会被物理碰撞阻挡的对象(host、slider、rail、
    shim、其它静态结构件);**sensor mesh 不应出现在 members 里**——
    sensor 是触发器件,不是 obstacle,把它的 mesh 打进 per-axis BVH
    会导致跨轴 collision 误判(本轴 slider 走到与另一轴 sensor 物理重叠
    的位置会被错判为撞了对方 axis 的 group)。请把 sensor 列到
    ``sensors`` 字段,引擎会单独记录它们的 object 名(sensor 自身的触发
    检测仍走 axis runtime,完全不依赖 collision engine)。
    """

    module_id: str
    host: object = None
    members: Tuple[object, ...] = ()
    bodies: Tuple[object, ...] = ()
    sensors: Tuple[object, ...] = ()


@dataclass(frozen=True)
class ResolvedStructure:
    """展开后的结构:组内对象名集合 + 运动体(引擎内部使用)。

    ``group_names`` 只含参与碰撞 BVH 的对象名;**sensor mesh 不在其中**,
    而是单独记在 :attr:`sensor_names` 里(供 dev 面板 / 命中细化 / 跨轴
    sensor 排除等地方使用,但绝不打进 per-axis BVH)。

    ``auto=True`` 表示这条结构不是模块声明的，而是引擎**遍历场景树时
    主动创建**的组合(见 :func:`build_auto_structures`):一个未被任何模块
    认领的根节点连同它的子树 = 一个组合结构,内部同样不做碰撞检测。
    自动组合的 ``host`` 恒为 ``None`` —— 它不是"声明的 host",因此不参与
    “相关组”跳过(否则一个模块就会不再与它所安装的静基准件做碰撞检测)。
    """

    module_id: str
    host: object = None
    host_name: Optional[str] = None
    group_names: frozenset = frozenset()
    sensor_names: frozenset = frozenset()
    body_names: Tuple[str, ...] = ()
    body_objects: Tuple[object, ...] = ()
    auto: bool = False


def legacy_structure(module) -> Optional[CollisionStructure]:
    """用旧字段名单推断一个模块的结构(未声明时的 fallback)。

    行为与重构前的 ``simulation_manager._collect_axis_object_names``
    等价:逐字段取值 → 解包 ``.obj`` → 以 ``sensors`` 列表里的对象为
    sensor(单独列,不进 members)。子树展开与 host 剪枝由引擎统一做,
    这里只列直接成员。
    """
    module_id = getattr(module, "module_id", None) or getattr(
        module, "axis_id", None
    )
    if module_id is None:
        return None

    members: list = []
    sensors: list = []
    seen: Set[int] = set()

    def _add(obj) -> None:
        if obj is None or id(obj) in seen:
            return
        seen.add(id(obj))
        members.append(obj)

    for field in LEGACY_MEMBER_FIELDS:
        try:
            _add(unwrap_object(getattr(module, field, None)))
        except ReferenceError:
            continue
    try:
        for sensor in getattr(module, "sensors", None) or ():
            obj = unwrap_object(getattr(sensor, "obj", sensor))
            if obj is not None:
                sensors.append(obj)
    except ReferenceError:
        pass

    bodies: list = []
    for field in LEGACY_BODY_FIELDS:
        try:
            obj = unwrap_object(getattr(module, field, None))
        except ReferenceError:
            obj = None
        if obj is not None:
            bodies.append(obj)
            break  # 旧实现只认一个运动体

    host = unwrap_object(getattr(module, "host_obj", None))
    return CollisionStructure(
        module_id=module_id,
        host=host,
        members=tuple(members),
        bodies=tuple(bodies),
        sensors=tuple(sensors),
    )


# ---- 子树展开 + host 剪枝 ----


def walk_name_subtree(
    obj, visited: Set[int], stop_names: Set[str] = frozenset()
) -> Set[str]:
    """返回以 ``obj`` 为根的子树里全部对象名(含自身)。

    ``stop_names`` 里的名字即"边界":遇到该名字的节点,**整棵子树都不
    进入**(也不包含该节点本身)。引擎用它实现"每个模块只认领自己的
    子树":其它已注册模块的 host 之下归那一组。
    """
    if obj is None:
        return set()
    name = object_name(obj)
    if name is not None and name in stop_names:
        visited.add(id(obj))
        return set()
    obj_id = id(obj)
    if obj_id in visited:
        return set()
    visited.add(obj_id)
    names: Set[str] = set()
    if name is not None:
        names.add(name)
    try:
        children = getattr(obj, "children", None) or ()
    except ReferenceError:
        children = ()
    for child in children:
        names |= walk_name_subtree(child, visited, stop_names)
    return names


def resolve_structure(
    structure: CollisionStructure,
    all_host_names: Iterable[str],
    *,
    extra_stop_names: Iterable[str] = (),
    auto: bool = False,
) -> ResolvedStructure:
    """把声明展开成"组内对象名集合 + 运动体",并施加剪枝。

    ``all_host_names``:其它已注册模块的 host 名(栅栏:整颗子树归别人)。
    ``extra_stop_names``:额外的栅栏名(引擎用它在自动创建组合结构时
    排掉已被**声明**认领的对象,避免双重归属)。
    """
    host_name = object_name(structure.host)
    stop_names = set(all_host_names) | set(extra_stop_names)
    if host_name is not None:
        stop_names.discard(host_name)

    # sensor mesh 单独展成 sensor_names —— 不加进 group_names。
    # 在 host 的子树展开里同样要跳出 sensor 子树: LinearAxis 的
    # sensor 通常是 host 的 child(以及 shim/slider 子孙中的 shim
    # mesh),如果不递显从子树中扣掉,walk_name_subtree 会把 sensor
    # 名字带进 group_names,造成“sensor 打进 BVH”的老 bug 复活。
    sensor_names: Set[str] = set()
    sensor_subtree_names: Set[str] = set()
    for sensor in getattr(structure, "sensors", ()) or ():
        sobj = unwrap_object(sensor)
        sn = object_name(sobj)
        if sn is not None:
            sensor_names.add(sn)
        # 子树展开(以 sensor 为根的子树同样要从 group 里扣掉)
        visited_sensor: Set[int] = set()
        sensor_subtree_names |= walk_name_subtree(
            sobj, visited_sensor, stop_names
        )
    stop_names |= sensor_subtree_names

    visited: Set[int] = set()
    group_names: Set[str] = set()
    for member in structure.members:
        group_names |= walk_name_subtree(member, visited, stop_names)
    # 双重保险:如果 sensor 被某个 member 的子展覆盖进 group_names,
    # 这里以名字集合减去 sensor 子树。保证 sensor 不可能出现在
    # per-axis group BVH 里。
    group_names -= sensor_subtree_names

    body_names: list = []
    body_objects: list = []
    for body in structure.bodies:
        bname = object_name(body)
        if bname is None:
            continue
        # 运动体必须算作组内成员,否则它会被当成障碍物/别组目标。
        group_names.add(bname)
        body_names.append(bname)
        body_objects.append(body)

    return ResolvedStructure(
        module_id=structure.module_id,
        host=structure.host,
        host_name=host_name,
        group_names=frozenset(group_names),
        sensor_names=frozenset(sensor_names),
        body_names=tuple(body_names),
        body_objects=tuple(body_objects),
        auto=auto,
    )


# ---- 场景树全量覆盖:自动组合结构(未声明的根节点) ----

# 自动创建的结构 id 前缀。带前缀是为了与模块声明的 ``module_id`` 永不碰撞,
# 也方便 UI / 日志一眼分辨“这是引擎自己包出来的组合”。
AUTO_ID_PREFIX: str = "structure:"


def auto_structure_id(root_name: str) -> str:
    """未声明根节点的组合结构 id。"""
    return AUTO_ID_PREFIX + str(root_name)


def is_auto_id(module_id: Optional[str]) -> bool:
    """该 id 是不是自动创建的组合结构。"""
    return bool(module_id) and str(module_id).startswith(AUTO_ID_PREFIX)


def build_auto_structures(
    scene_objects: Iterable,
    claimed_names: Iterable[str],
    all_host_names: Iterable[str],
    *,
    is_candidate=None,
) -> list:
    """把场景树里**未被任何声明认领**的根节点各自打包成一个组合结构。

    算法(自上而下,保证不漏对象):

    1. ``stop = claimed_names | all_host_names`` —— 已被声明认领的对象、
       以及其它模块的 host 子树全部不进入自动组合。
    2. 遍历场景对象,找出"未认领且其父节点也不在未认领集里"的节点 → 它
       就是一条**未认领子树的根**;已经归在某个未认领根下的后代不会被
       重复建结构(随父的组合一起被 ``walk_name_subtree`` 走至)。
    3. 每个这样的根建一个 ``ResolvedStructure``:成员 = 根 + 子树(遇
       ``stop`` 剪枝),``host=None``,``auto=True``,``bodies=()``。
       因为它是静基准件/装饰物,本仓库不驱动它运动,所以没有运动体。
    4. 只有“至少包含一个候选碰撞物”的组合才保留(纯 EMPTY / 相机 /
       全隐藏 / 无 polygon 的根不建无用结构);``is_candidate`` 为
       ``None`` 时不做这个过滤。

    返回自动结构的列表(顺序与 ``scene_objects`` 一致,保证 id / 结果稳定)。
    """
    objects = [o for o in (scene_objects or ()) if o is not None]
    stop = set(claimed_names) | set(all_host_names)

    # 名字 → 对象(只含当前场景对象;父不在场景里的对象当根处理)
    by_name: Dict[str, object] = {}
    for obj in objects:
        name = object_name(obj)
        if name is not None:
            by_name.setdefault(name, obj)

    unclaimed: Set[str] = set()
    for obj in objects:
        name = object_name(obj)
        if name is not None and name not in stop:
            unclaimed.add(name)

    out: list = []
    for obj in objects:
        name = object_name(obj)
        if name is None or name not in unclaimed:
            continue
        parent = object_parent(obj)
        parent_name = object_name(parent)
        if parent_name is not None and parent_name in unclaimed:
            # 父节点也未被认领 → 会随父的组合一起被走至,这里不重复建
            continue
        structure = CollisionStructure(
            module_id=auto_structure_id(name),
            host=None,
            members=(obj,),
            bodies=(),
        )
        resolved = resolve_structure(
            structure,
            all_host_names,
            extra_stop_names=stop,
            auto=True,
        )
        # 展示用名字保留根名(Dev panel / 命中记录都靠它),但 ``host`` 保持
        # None —— 这样 R2(相关组跳过)不会把静态基准件也跳掉。
        resolved = replace(resolved, host_name=name)
        if is_candidate is not None and not any(
            is_candidate(by_name[n])
            for n in resolved.group_names
            if n in by_name
        ):
            continue
        out.append(resolved)
    return out


# ---- 组关系 ----


def host_chain_depth(obj, limit: int = 256) -> list:
    """返回 ``obj`` 向上的祖先链(含自身),防环。"""
    chain: list = []
    cur = obj
    seen: Set[int] = set()
    while cur is not None and len(chain) < limit:
        cid = id(cur)
        if cid in seen:
            break
        seen.add(cid)
        chain.append(cur)
        cur = object_parent(cur)
    return chain


def hosts_are_related(host_a, host_b) -> bool:
    """两个 host 是否在场景树上有祖孙关系(含同一对象)。"""
    if host_a is None or host_b is None:
        return False
    if host_a is host_b:
        return True
    return any(node is host_b for node in host_chain_depth(host_a)) or any(
        node is host_a for node in host_chain_depth(host_b)
    )


def groups_are_related(
    resolved: Dict[str, ResolvedStructure], module_a: Optional[str],
    module_b: Optional[str],
) -> bool:
    """两个模块的组是否“物理上挂在一起”(host 祖孙关系)。

    相关组整对跳过:例如 Cylinder 的 host 挂在 LinearAxis 的 slider 下、
    真空吸嘴挂在 work bar 下 —— 它们本来就一起运动,互相重叠不是碰撞。

    注意:**自动创建的组合结构(`auto=True`,host=None)永不参与这条规则**。
    理由:静基准件/装饰物本身不运动,挂在它上面的模块相对它有真实移动 ——
    压在基准件上就是真碰撞,不能被 ancestry 关系跳过。
    """
    if module_a is None or module_b is None:
        return False
    if module_a == module_b:
        return True
    a = resolved.get(module_a)
    b = resolved.get(module_b)
    if a is None or b is None:
        return False
    if getattr(a, "auto", False) or getattr(b, "auto", False):
        return False
    return hosts_are_related(a.host, b.host)


# ---- 结构指纹 ----


def pose_digest(obj) -> tuple:
    """对象的位姿摘要(位移 + 旋转)。取不到时返回 ``()``。

    只用于"是否变化"的比较,不做数值运算。宽度按 :data:`_POSE_PRECISION`
    四舍五入,避免浮点噪声导致无谓重建。
    """
    digest: list = []
    mw = getattr(obj, "matrix_world", None)
    if mw is not None:
        try:
            loc = getattr(mw, "translation", None)
            if loc is not None:
                digest.extend(
                    round(float(loc[i]), _POSE_PRECISION) for i in range(3)
                )
        except Exception:
            pass
        try:
            quat = mw.to_quaternion()
            digest.extend(
                round(float(quat[i]), _POSE_PRECISION) for i in range(4)
            )
        except Exception:
            pass
    if not digest:
        try:
            loc = getattr(obj, "location", None)
            if loc is not None:
                digest.extend(
                    round(float(loc[i]), _POSE_PRECISION) for i in range(3)
                )
        except Exception:
            pass
    return tuple(digest)


def structure_fingerprint(
    scene_objects: Iterable,
    resolved: Dict[str, ResolvedStructure],
    pose_tracked: Iterable[str] = (),
) -> tuple:
    """结构指纹:结构变化(分组/父子/对象增删)即刻改变。

    ``pose_tracked`` 里的对象名额外参与**位姿**比对 —— 引擎只把
    "不属于任何组的独立障碍物"放进去(它们通常是静止摆设,一旦被挪动
    就能立即生效)。

    运动体与组内成员**故意不参与位姿比对**:

    - 运动体每 tick 都会动,位姿进指纹会导致每 tick 重建整张缓存
      (等于退回“每 tick 全量重建”的高开销);
    - 组内成员的位姿由重建节奏(``rebuild_every_n_ticks``,默认 30)
      兵底 —— 与重构前一致,不是本方案要解的问题。
    """
    tracked = set(pose_tracked)
    parts: list = []
    objects = list(scene_objects or ())
    parts.append(("count", len(objects)))
    for obj in objects:
        name = object_name(obj)
        if name is None:
            continue
        digest = pose_digest(obj) if name in tracked else ()
        parts.append(
            ("obj", name, object_name(object_parent(obj)), digest)
        )
    for module_id in sorted(resolved):
        st = resolved[module_id]
        parts.append(
            (
                "group",
                module_id,
                st.host_name,
                tuple(sorted(st.group_names)),
                tuple(sorted(st.body_names)),
                tuple(sorted(getattr(st, "sensor_names", ()))),
            )
        )
    return tuple(parts)


__all__ = [
    "LEGACY_MEMBER_FIELDS",
    "LEGACY_BODY_FIELDS",
    "AUTO_ID_PREFIX",
    "CollisionStructure",
    "ResolvedStructure",
    "object_name",
    "object_parent",
    "unwrap_object",
    "is_rigid_body_object",
    "legacy_structure",
    "walk_name_subtree",
    "resolve_structure",
    "auto_structure_id",
    "is_auto_id",
    "build_auto_structures",
    "host_chain_depth",
    "hosts_are_related",
    "groups_are_related",
    "pose_digest",
    "structure_fingerprint",
]