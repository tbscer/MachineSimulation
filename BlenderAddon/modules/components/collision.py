"""Physics-engine collision detection for axis sliders vs scene obstacles.

This module wraps :class:`mathutils.bvhtree.BVHTree` (the same BVH
accelerator Bullet uses internally) to detect mesh-vs-mesh contact between
an axis's moving body (a ``Slider`` or ``Rotator`` mesh) and every other
mesh in the scene that is not part of any registered axis. The
:class:`CollisionEngine` is shared across all axis runtimes and is the
single source of truth for the scene-level
``scene["motion_simulation_collision"]`` marker.

Why BVH, not AABB
-----------------
Pure-Python AABB overlap (the existing :class:`SensorComponent` approach)
is too conservative for complex meshes — a long thin slider will overlap
the AABB of a large obstacle long before it actually touches the mesh.
``BVHTree.FromObject`` is the same data structure Blender's rigid body
solver uses for its broadphase, so the precision matches what
``bpy.types.RigidBodyObject`` would deliver, without the simulation
response we explicitly do not want on kinematic bodies.

Why scene-level marker, not per-axis
------------------------------------
The user-facing contract is: when any axis collides, **all** axes
stop. The single scene-level boolean is the cheapest signal for an
external controller (MCP, RPC, UI) to read, and it removes the need to
fan out the collision event across modules. Per-axis detail is exposed
through the existing ``axis_state == "blocked"`` mirror.

集中式拉模式(2025 重构)
-------------------------
本模块是碰撞的**唯一计算入口**。每个 tick 由
:meth:`SimulationManager.update` 调用一次 :meth:`CollisionEngine.step`:
engine 自己从模块拉取 3D 结构声明
(:meth:`BaseSimulationModule.collision_structure`),自己做分组、重建
缓存与全部重叠计算,命中后通过 :meth:`on_collision_hit` 通知模块。
模块不再自己调用 ``check_slider_collision`` —— 三段重复在每个模块
``update()`` 里的检查循环已删除(仅在引擎不管理模块名册的旧式测试
替身上保留 fallback 路径,见 ``module._legacy_collision_check``)。

不碰撞的判定统一为两条规则(实现见 :mod:`collision_structure`):

- **组内不检测**:同一个 module 声明的所有对象(host + 部件 + 声明的
  工件)之间永不做碰撞检测。
- **相关组不检测**:两个模块的 host 在场景树上有祖孙关系(物理上挂
  在一起,如 Cylinder 挂在 LinearAxis 上、吸嘴挂在 work bar 上)→
  整对跳过。

缓存失效不再依靠固定 tick 节奏,而是每 tick 比对一次
**结构指纹**(分组 + 父子关系 + 位姿):指纹变了立即重建,使
运行时 reparent/attach 在同一 tick 内生效。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

try:  # Blender 加载路径
    from .collision_structure import (
        AUTO_ID_PREFIX,
        CollisionStructure,
        ResolvedStructure,
        build_auto_structures,
        groups_are_related as _groups_are_related_names,
        hosts_are_related as _hosts_are_related,
        is_auto_id as _is_auto_id,
        legacy_structure,
        object_name as _object_name,
        resolve_structure,
        structure_fingerprint,
    )
except ImportError:  # pragma: no cover - 离线/直接脚本 import 路径
    from modules.components.collision_structure import (  # type: ignore
        AUTO_ID_PREFIX,
        CollisionStructure,
        ResolvedStructure,
        build_auto_structures,
        groups_are_related as _groups_are_related_names,
        hosts_are_related as _hosts_are_related,
        is_auto_id as _is_auto_id,
        legacy_structure,
        object_name as _object_name,
        resolve_structure,
        structure_fingerprint,
    )


# 2b 豁免:"刚体 vs 刚体"识别(duck-typing,离线 mock 亦可用)
try:
    from .collision_structure import (
        is_rigid_body_object as _is_rigid_body_object,
    )
except ImportError:  # pragma: no cover - 离线/直接脚本 import 路径
    from modules.components.collision_structure import (  # type: ignore
        is_rigid_body_object as _is_rigid_body_object,
    )


# Scene-level custom property key. External readers (MCP ``execute_code``,

# RPC subscribers, the 3D-viewport UI) check this to learn that motion
# has been halted by contact with an obstacle.
SCENE_COLLISION_KEY: str = "motion_simulation_collision"

# Stop-reason vocabulary string. Mirrors the existing ``"limit"`` /
# ``"homed"`` / ``"arrived"`` strings written to ``axis_stop_reason``.
STOP_REASON_COLLISION: str = "collision"


def _fan_triangulate(mesh):
    """三角化 mesh 的所有 polygon,支持矩形环(外圈矩形 + 内圈矩形洞)。

    在 Blender 5.x 中, :meth:`BVHTree.FromPolygons` 拒绝非三角形输入
    (4.x 时的 ``all_triangles=True`` 自动切分在 5.x 已移除)。本函数
    是 :func:`_build_bvh` / :func:`_build_combined_bvh` 的预处理步骤。

    参数
    ----
    ``mesh`` : :class:`bpy.types.Mesh`
        要三角化的 mesh。

    返回
    ----
    ``list`` of ``(v0, v1, v2)``
        三角形顶点索引三元组,索引引用 ``mesh.vertices``。

    设计要点 —— 为什么不能用 fan 切矩形环
    ------------------------------------
    Blender 5.x 的 ``mesh.polygons[i].vertices`` **包含内圈 vertex**
    (不再是只列外圈)—— 实测对带洞 face 返回 8 个 vertex(外圈 4 +
    内圈 4)。如果直接 fan,8-loop 会被切出 6 个跨越洞的三角形,
    等同于把洞填实:这是历史 bug 的根因,用户挖了洞的底座
    (例如 ``Base.Y``)被识别为实心方块,运动体停在"洞里"也会被误报撞。

    Blender 5.x 还删除了 :attr:`BMFace.holes` API, ``bmesh.ops.triangulate``
    不感知 hole,仍按 fan 切。当前采用的特化方案:

    - 对每个 polygon 取 ``mesh.loops[loop_start : loop_start + loop_total]``
      作为完整 loop 序列;
    - 若 X/Y 各 4 个极值 + 4 outer / 4 inner corner 完整 → 矩形环,按
      :func:`_triangulate_rect_ring` 切 8 三角形(顶/底/左/右 4 矩形条);
    - 否则 → fan(对无洞 polygon 仍正确,对非矩形洞会填洞,需要
      用户在 Blender 把 face 拆成多个简单 face)。

    性能
    ----
    对无洞 polygon (Rail.Y / Sensor / Slide.Y 等)仍走 fan,
    O(n) 三角化无额外开销;矩形环多一次 X/Y 分桶比较,
    8 个 vertex 也只是常数开销。
    """
    if not getattr(mesh, "polygons", None):
        return []

    out: list = []
    for poly in mesh.polygons:
        loops = [mesh.loops[i].vertex_index
                 for i in range(poly.loop_start, poly.loop_start + poly.loop_total)]
        # 优先尝试矩形环特化
        ring_tris = _triangulate_rect_ring(loops, mesh.vertices)
        if ring_tris is not None:
            out.extend(ring_tris)
            continue
        # 简单 polygon:fan
        verts = poly.vertices if hasattr(poly, "vertices") else tuple(poly)
        n = len(verts)
        if n == 3:
            out.append(tuple(verts))
        elif n > 3:
            for i in range(1, n - 1):
                out.append((verts[0], verts[i], verts[i + 1]))
    return out


def _triangulate_rect_ring(loops, mesh_verts):
    """矩形环特化:8 loop = 4 outer corner + 4 inner corner,按 4 矩形条切 8 三角形。

    识别条件
    --------
    - 恰好 8 个 loop;
    - 8 个 vertex 坐标的 X 有 4 个极值、Y 有 4 个极值;
    - 4 个 outer corner 坐标 = (Xmin/Xmax, Ymin/Ymax) 笛卡尔积的 4 个;
    - 4 个 inner corner 坐标 = 中间两个 X × 中间两个 Y。

    不满足条件时返回 ``None``,调用方降级 fan。

    切分
    ----
    矩形环拆为 顶/底/左/右 4 个矩形条,每条 2 三角形 = 8 三角形。
    这 8 个三角形 union = 矩形环(外圈矩形 − 内圈矩形),**不包含洞**。
    """
    if len(loops) != 8:
        return None

    coords = [mesh_verts[i].co for i in loops]

    # 用 round(..., 4) 容错,避免浮点噪声
    xs = sorted({round(c.x, 4) for c in coords})
    ys = sorted({round(c.y, 4) for c in coords})
    if len(xs) != 4 or len(ys) != 4:
        return None

    outer_x_lo, inner_x_lo, inner_x_hi, outer_x_hi = xs
    outer_y_lo, inner_y_lo, inner_y_hi, outer_y_hi = ys

    # 4 个 outer corner = (X∈{outer_x_lo, outer_x_hi}, Y∈{outer_y_lo, outer_y_hi})
    outer_corners = {}
    inner_corners = {}
    for vi, c in zip(loops, coords):
        x, y = round(c.x, 4), round(c.y, 4)
        if x in (outer_x_lo, outer_x_hi) and y in (outer_y_lo, outer_y_hi):
            key = (x, y)
            if key not in outer_corners:
                outer_corners[key] = vi
        elif x in (inner_x_lo, inner_x_hi) and y in (inner_y_lo, inner_y_hi):
            key = (x, y)
            if key not in inner_corners:
                inner_corners[key] = vi

    if len(outer_corners) != 4 or len(inner_corners) != 4:
        return None

    o00 = outer_corners[(outer_x_lo, outer_y_lo)]
    o01 = outer_corners[(outer_x_lo, outer_y_hi)]
    o10 = outer_corners[(outer_x_hi, outer_y_lo)]
    o11 = outer_corners[(outer_x_hi, outer_y_hi)]
    i00 = inner_corners[(inner_x_lo, inner_y_lo)]
    i01 = inner_corners[(inner_x_lo, inner_y_hi)]
    i10 = inner_corners[(inner_x_hi, inner_y_lo)]
    i11 = inner_corners[(inner_x_hi, inner_y_hi)]

    # 4 个矩形条:
    #   顶条: o01 → o11 → i11 → i01   (上方外圈到内圈)
    #   底条: o00 → i00 → i10 → o10   (下方外圈到内圈)
    #   左条: o00 → o01 → i01 → i00   (左侧外圈到内圈)
    #   右条: o10 → i10 → i11 → o11   (右侧外圈到内圈)
    # 每条 2 三角形 = 8 三角形覆盖矩形环。
    return [
        # 顶条
        (o01, o11, i11),
        (o01, i11, i01),
        # 底条
        (o00, i00, i10),
        (o00, i10, o10),
        # 左条
        (o00, o01, i01),
        (o00, i01, i00),
        # 右条
        (o10, i10, i11),
        (o10, i11, o11),
    ]


# ---- pure helpers (no bpy import; unit-testable offline) ----


def is_obstacle_candidate(obj, exclusion_names: Set[str]) -> bool:
    """Return True iff ``obj`` should be considered an obstacle.

    Excluded classes:
    - any object whose name appears in ``exclusion_names`` (i.e. belongs
      to a registered axis — host, slider, rail, shim, sensor);
    - anything that is not a ``MESH``;
    - meshes with no polygon data (empty / unevaluated);
    - meshes hidden in viewport or render.

    Note: Blender's per-object ``collision.use`` flag (the toggle for
    Blender's own rigid-body / cloth collision system) is **not**
    consulted here. That flag is independent of this addon's
    custom BVH-based collision detection: a mesh the artist has
    marked ``collision.use = False`` (often for performance when
    the object is purely decorative) is still a valid obstacle
    for our purposes, and the user has explicitly asked for any
    mesh they place in the scene to be a candidate.

    Defensive against objects that have lost their ``.type`` attribute
    (e.g. weakrefs to removed datablocks during teardown).
    """
    if obj is None:
        return False
    name = getattr(obj, "name", None)
    if name is None or name in exclusion_names:
        return False
    if getattr(obj, "type", None) != "MESH":
        return False
    data = getattr(obj, "data", None)
    if data is None:
        return False
    try:
        if len(data.polygons) == 0:
            return False
    except Exception:
        return False
    if getattr(obj, "hide_viewport", False):
        return False
    if getattr(obj, "hide_render", False):
        return False
    return True


def bvh_overlap_exists(bvh_a, bvh_b) -> bool:
    """Return True if two BVHTrees have any triangle overlap.

    This is a thin wrapper over :meth:`BVHTree.overlap` so the rest of
    the engine can be unit-tested with hand-built trees. Returns False
    on either tree being ``None`` (graceful degradation) so callers
    don't need to guard.
    """
    if bvh_a is None or bvh_b is None:
        return False
    try:
        pairs = bvh_a.overlap(bvh_b)
    except Exception:
        return False
    return bool(pairs)


def current_depsgraph():
    """取当前 evaluated depsgraph(无 bpy / 取不到时返回 ``None``)。"""
    try:
        import bpy
        return bpy.context.evaluated_depsgraph_get()
    except Exception:
        return None


def module_self_check(engine, module, body_obj, depsgraph) -> bool:
    """过渡期兼容入口:模块自己驱动一次碰撞检查。

    集中式拉模式(:meth:`CollisionEngine.step`)下**不调用**这里 —— 模块
    不应该自己发起碰撞计算。本函数只在以下场景生效:

    - 引擎未接管模块名册(``engine.handles_modules is False``),典型
      情况是离线单元测试替身与外部脚本;
    - 引擎为 ``None`` 或运动体为空 → 直接返回 ``False``。

    行为与重构前的三段内联代码完全一致:按节奏刷新障碍缓存 →
    ``check_slider_collision`` → 只有当引擎启用时才返回 ``True``
    (即使禁用也要算一次,以便 Dev panel 继续显示“会撞到什么”)。
    返回 ``True`` 表示调用方应该进入 BLOCKED。
    """
    if engine is None or body_obj is None or depsgraph is None:
        return False
    if getattr(engine, "handles_modules", False):
        return False
    module_id = getattr(module, "module_id", None) or getattr(
        module, "axis_id", None
    )
    try:
        engine.rebuild_if_due(depsgraph, axis_id=module_id)
    except TypeError:
        try:
            engine.rebuild_if_due(depsgraph)
        except Exception:
            pass
    except Exception:
        pass
    try:
        hit = bool(
            engine.check_slider_collision(
                body_obj, depsgraph, axis_id=module_id
            )
        )
    except Exception:
        return False
    if not hit:
        return False
    # 2b:刚体 vs 刚体的工艺接触(kind="rigid_contact")不进 BLOCKED
    try:
        if bool((getattr(engine, "last_collision", None) or {}).get("exempt")):
            return False
    except Exception:
        pass
    if not getattr(engine, "is_enabled", True):
        return False
    try:
        engine.mark_collision()
    except Exception:
        pass
    return True


# ---- engine ----


class CollisionEngine:
    """Per-tick mesh-vs-mesh collision detection for all axis runtimes.

    The engine holds a per-tick BVH cache of the scene's obstacle meshes
    and answers ``check_slider_collision(slider_obj, depsgraph)`` for
    every axis. The scene-level marker
    ``scene["motion_simulation_collision"]`` is written through
    :meth:`mark_collision` and cleared through :meth:`clear_collision`.

    Construction
    ------------
    The engine is constructed once in :mod:`addon` after
    :class:`SimulationManager`. The axis-names provider is attached
    later so the engine can lazily fetch the exclusion set without
    importing the manager at module load time.
    """

    def __init__(self, scene, *, rebuild_every_n_ticks: int = 30) -> None:
        if rebuild_every_n_ticks < 1:
            raise ValueError("rebuild_every_n_ticks must be >= 1")
        self._scene = scene
        self._rebuild_every_n_ticks = int(rebuild_every_n_ticks)
        # Per-axis cadence counters，key 为 ``axis_id``（未提供时用
        # ``"__global__"``）。每个 axis 各自的计数达到
        # ``rebuild_every_n_ticks`` 就触发一次重建，**任何一次真正发生的
        # 重建都会清空全部计数**，所以无论场景里有多少个 axis，缓存都是
        # 每 ``rebuild_every_n_ticks`` 个 tick 重建一次。
        #
        # 旧实现只用一个全局 ``_tick_counter``，而每个 axis 每 tick 都会调
        # 一次 ``rebuild_if_due``，导致 N 个 axis 时重建周期变成
        # ``30 / N`` tick（3 个轴 → 每 10 tick 重建一次）：既与文档声称的
        # "每 30 tick 一次" 不符，又多烧了几倍 CPU。
        self._tick_counters: dict = {}
        # Per-axis combined BVH cache: ``_axis_bvhs[axis_id]`` is a
        # single BVHTree containing the world-space geometry of every
        # object that belongs to that axis (host, slider, rail, shim,
        # sensors, child meshes, …). The slider is checked against
        # each axis-as-unit BVH in :meth:`check_slider_collision` —
        # the per-axis exclusion becomes implicit: the slider's own
        # axis BVH is the one that's skipped, not individual object
        # names.
        self._axis_bvhs: dict = {}
        # Per-object BVH cache for non-axis obstacles (any mesh the
        # artist added that is not part of a registered axis). The
        # slider's BVH is checked against each of these too.
        self._obstacle_cache: dict = {}  # obj_name -> BVHTree
        # 注意：per-axis 排除**没有**查询期的名字缓存 dict。
        # ``_rebuild_obstacle_cache`` 通过 ``_per_axis_provider`` 拿到每个
        # axis 的 owned 名字集后直接构建 ``_axis_bvhs[axis_id]``；
        # ``check_slider_collision`` 只做 BVH 比较，不再做名字匹配。
        # （历史上有一个 ``_per_axis_exclusion`` dict 配一对
        # ``set/get_per_axis_exclusion`` 方法，全项目无人调用，已删除。）
        # Backward-compat flat union of all axis object names. Kept
        # for any caller that hasn't been updated to the per-axis
        # path; ``_rebuild_obstacle_cache`` populates it from the
        # union of the per-axis sets.
        self._exclusion_cache: set = set()
        self._axis_names_provider: Optional[Callable[[], Set[str]]] = None
        # Per-axis provider: ``provider(axis_id) -> set[str]``. Set
        # by the addon to ``manager.axis_object_names``.
        self._per_axis_provider: Optional[Callable[[str], Set[str]]] = None
        # Last collision observation — written by check_slider_collision
        # on a hit, cleared by the user (or by mark/clear_collision).
        # Exposed read-only via :attr:`last_collision` for the Dev panel.
        self._last_collision: Optional[dict] = None
        # Per-axis host_obj cache. Refreshed at every
        # ``_rebuild_obstacle_cache`` so ancestry checks in
        # ``check_slider_collision`` stay current.
        #
        # Two axes are "related" iff their hosts stand in an
        # ancestor-descendant relationship in the scene tree —
        # e.g. a Y-axis host parented to an X-axis slider. The Y
        # axis is mounted on the X rig and physically moves with
        # it; an overlap between an X body and a Y body is the rig
        # bumping into its own payload, not a real collision.
        self._axis_host_map: Dict[str, object] = {}
        # ---- 集中式拉模式(新增)----
        # 模块名册提供者:``provider() -> list[module]``。由 addon 注入
        # ``manager.all``。装了它才进入拉模式;未装时退化为旧式
        # provider 兼容路径(tests / 外部脚本)。
        self._module_source: Optional[Callable[[], list]] = None
        # 本 tick 解析后的结构:``module_id -> ResolvedStructure``。
        self._structures: Dict[str, ResolvedStructure] = {}
        # module_id -> module 实例(通知钩子用)。
        self._modules_by_id: Dict[str, object] = {}
        # 上一个 tick 的结构指纹;None = 还没算过。
        self._fingerprint: Optional[tuple] = None
        # 本 tick 的命中记录:``module_id -> detail``。
        self._hits: Dict[str, dict] = {}
        # 距离上次重建走了多少个 step()(仅用于“位姿兜底”的节奏)。
        self._step_ticks: int = 0
        # 覆盖率自检:候选 mesh 里没落入任何组合的对象名(应为空)。
        self._uncovered: List[str] = []
        # BVH 缓存重建次数(指纹命中率测试/巡检用)。
        self._rebuild_count: int = 0
        # When False, ``mark_collision`` is a no-op and
        # ``read_marker`` returns False. The Dev panel exposes a
        # ``Disable / Enable Collision`` toggle that flips this.
        # This is the artist-facing override for the
        # common "the rig bumped its own payload" false positive:
        # the user can park the rig for inspection without
        # unblocking every tick.
        self._enabled: bool = True

    # ---- configuration ----

    def attach_axis_names_provider(self, provider: Callable[[], Set[str]]) -> None:
        """Install a callable returning the flat union of all axis object names.

        Kept for backward compatibility. Prefer
        :meth:`attach_per_axis_provider` for the per-axis path.
        """
        self._axis_names_provider = provider

    def attach_per_axis_provider(self, provider: Callable[[str], Set[str]]) -> None:
        """Install a callable returning per-axis object names.

        ``provider(axis_id)`` returns the set of Object names owned
        by that axis. Used to skip same-axis collisions while
        allowing cross-axis collisions. Set by the addon to
        ``manager.axis_object_names``.

        Deprecated: 已被 :meth:`attach_module_source` + 模块自己的
        ``collision_structure()`` 取代。仅在引擎不管理模块名册时
        (旧式测试/外部脚本)仍被 :meth:`_per_group_names` 使用。
        """
        self._per_axis_provider = provider

    def attach_module_source(self, provider: Callable[[], list]) -> None:
        """安装模块名册提供者:``provider() -> list[module]``。

        装上它即进入**集中式拉模式**:engine 每 tick 自己遍历这批模块,
        向每个模块要 :class:`CollisionStructure`,自己完成全部重叠计算与
        命中通知。模块侧的旧式自检分支会因 ``handles_modules is True``
        而自动关闭(见 ``module._legacy_collision_check``)。
        """
        self._module_source = provider

    @property
    def handles_modules(self) -> bool:
        """engine 是否接管了模块名册(拉模式)。

        模块用它决定是否需要跑自己的旧式自检分支:拉模式下 engine 会
        通过 ``on_collision_hit`` 通知,模块不需要(也不应该)再调
        ``check_slider_collision``。
        """
        return self._module_source is not None

    # ---- scene marker ----

    def _get_live_scene(self):
        """Return the current active scene, recovering from a stale
        reference.

        The engine holds a reference to the scene it was constructed
        with, but Blender may replace the active scene (open a new
        ``.blend``, ``bpy.ops.wm.read_factory_settings``, etc.) at any
        time. The stored reference then becomes a ``StructRNA`` whose
        access raises ``ReferenceError: StructRNA of type Scene has
        been removed``.

        To stay robust, every scene access goes through this helper:
        the cached reference is tried first, and on ``ReferenceError``
        we re-fetch from ``bpy.context.scene`` (which Blender keeps
        pointing at the active scene) and cache the new reference.
        A missing ``bpy`` (offline tests) or a still-stale scene
        returns ``None`` so callers can no-op.

        The "is the scene still alive" probe is a duck-typed ``name``
        attribute access. Stand-in mocks (e.g. ``FakeScene`` in the
        unit tests) that don't expose ``name`` are treated as live
        — the offline test harness does not import Blender, so the
        ``bpy.context.scene`` re-acquisition path is never taken and
        the mock scene is used as-is.
        """
        # Fast path: the cached reference is still alive.
        if self._scene is not None:
            try:
                if hasattr(self._scene, "name"):
                    _ = self._scene.name  # touch a property to validate
                return self._scene
            except ReferenceError:
                self._scene = None
        # Recover: re-fetch the active scene from the Blender context.
        try:
            import bpy  # local import — keep this module import-safe
            new_scene = bpy.context.scene
        except Exception:
            return None
        if new_scene is None:
            return None
        try:
            if hasattr(new_scene, "name"):
                _ = new_scene.name
        except ReferenceError:
            return None
        self._scene = new_scene
        return self._scene

    def mark_collision(self) -> None:
        """Set the scene-level collision flag. Idempotent.

        When the engine is :attr:`disabled` via :meth:`set_disabled`,
        this is a no-op so the artist can park the rig for
        inspection without the collision loop re-asserting the
        block every tick.
        """
        if not self._enabled:
            return
        scene = self._get_live_scene()
        if scene is None:
            return
        try:
            scene[SCENE_COLLISION_KEY] = True
        except ReferenceError:
            # Scene replaced mid-write. Drop the cache and try once
            # more against the new active scene.
            self._scene = None
            scene = self._get_live_scene()
            if scene is None:
                return
            try:
                scene[SCENE_COLLISION_KEY] = True
            except Exception:
                pass

    def clear_collision(self) -> None:
        """Clear the scene-level collision flag. Idempotent."""
        scene = self._get_live_scene()
        if scene is None:
            return
        try:
            scene[SCENE_COLLISION_KEY] = False
        except ReferenceError:
            self._scene = None
            scene = self._get_live_scene()
            if scene is None:
                return
            try:
                scene[SCENE_COLLISION_KEY] = False
            except Exception:
                pass
        # Also drop the last-collision record so the Dev panel
        # doesn't keep showing a stale "axis X hit obstacle Y" line.
        self._last_collision = None

    @property
    def last_collision(self) -> Optional[dict]:
        """Return the most-recent collision record, or ``None``.

        The record is a plain ``dict`` with the keys ``axis_id``,
        ``obstacle_name``, ``slider_name``. Any field can be ``None``
        (e.g. ``axis_id`` is only set when the engine is queried
        through the per-axis code path that knows which axis it
        belongs to).
        """
        return self._last_collision

    def read_marker(self) -> bool:
        """Return True iff the scene-level collision flag is currently set.

        When the engine is :attr:`disabled`, returns False
        unconditionally. This is what makes the per-axis tick
        branch treat the rig as not-blocked: ``axis.update`` calls
        ``engine.read_marker()`` and only enters ``STATE_BLOCKED``
        on True, so a disabled engine keeps the rig free even if a
        BVH overlap would otherwise be detected.
        """
        if not self._enabled:
            return False
        scene = self._get_live_scene()
        if scene is None:
            return False
        try:
            return bool(scene.get(SCENE_COLLISION_KEY, False))
        except ReferenceError:
            self._scene = None
            return False
        except Exception:
            return False

    def set_disabled(self, disabled: bool) -> bool:
        """Toggle the engine's enabled flag.

        When ``disabled`` is True:
        - ``mark_collision`` becomes a no-op.
        - ``read_marker`` returns False (axes are released from
          STATE_BLOCKED on the next tick).
        - ``check_slider_collision`` still runs and updates
          ``last_collision`` for the Dev panel, but it does not
          write the scene flag.
        - 两个轴运行时（LinearAxis / RotateAxisRuntime）在检测到命中时还会
          检查 :attr:`is_enabled`：引擎被关闭时，即使运动体仍与障碍几何重叠
          （美术端把 rig 停在障碍里“停车检查”的典型场景），也不会重新进入
          STATE_BLOCKED——禁用是一次真正的“kill switch”，而不是只停掉
          scene marker。

        Returns True iff the flag actually changed.
        """
        new_state = not bool(disabled)
        changed = (new_state != bool(self._enabled))
        self._enabled = new_state
        # Always clear the scene-level flag so a stale True from a
        # previous tick doesn't keep axes pinned in STATE_BLOCKED.
        try:
            self.clear_collision()
        except Exception:
            pass
        if not new_state:
            # Disable path: force every currently-BLOCKED module back
            # to IDLE *now*, not on the next tick.
            #
            # 集中式拉模式用显式钩子 :meth:`BaseSimulationModule.on_collision_cleared`;
            # 旧模块上的 ``_clear_blocked_state_only`` 作为兼容名保留。
            # 模块名册优先取本 tick 拉到的结构(不再反向依赖 addon 单例);
            # 没有名册时(旧式外部脚本)才退回 manager。
            modules = list(self._modules_by_id.values())
            if not modules:
                try:
                    from ... import addon as _addon
                    _mgr = _addon.get_manager()
                except Exception:
                    _mgr = None
                if _mgr is not None:
                    modules = list(_mgr.all())
            for module in modules:
                if getattr(module, "state", None) != "blocked":
                    continue
                release = getattr(module, "on_collision_cleared", None)
                if release is None:
                    release = getattr(
                        module, "_clear_blocked_state_only", None
                    )
                if release is None:
                    continue
                try:
                    release()
                except Exception:
                    pass
        return changed

    @property
    def is_enabled(self) -> bool:
        return bool(self._enabled)

    # ---- cache management ----

    def rebuild_if_due(self, depsgraph, axis_id: Optional[str] = None) -> bool:
        """Rebuild the obstacle BVH cache if the cadence counter has elapsed.

        The counter is **per axis** (``axis_id``), so one engine shared by
        several modules still rebuilds once per ``rebuild_every_n_ticks``
        *ticks* instead of once per axis-call: an axis's own counter only
        reaches the threshold after that many of its own ticks, and every
        real rebuild resets all counters so the axes stay in phase.

        ``axis_id`` defaults to ``None`` for legacy callers / offline
        tests; those share a single ``"__global__"`` counter and therefore
        keep the old "every N calls" cadence.

        Returns True iff a rebuild actually happened this call. Safe to
        call on every tick: most calls are no-ops.
        """
        key = axis_id if axis_id is not None else "__global__"
        self._tick_counters[key] = self._tick_counters.get(key, 0) + 1
        if self._tick_counters[key] < self._rebuild_every_n_ticks:
            return False
        self._tick_counters.clear()
        self._rebuild_obstacle_cache(depsgraph)
        return True

    def force_rebuild(self, depsgraph) -> None:
        """Immediately rebuild the obstacle BVH cache.

        Called after discovery / re-discovery so newly-added or removed
        obstacles / axes take effect on the next tick rather than
        waiting up to ``rebuild_every_n_ticks`` ticks. Also clears every
        per-axis cadence counter, so the next scheduled rebuild happens a
        full cadence after this one.
        """
        self._tick_counters.clear()
        self._rebuild_obstacle_cache(depsgraph)

    def _rebuild_obstacle_cache(self, depsgraph) -> None:
        """Walk the scene and build the per-axis + per-object BVH caches.

        Two caches are produced:
        - ``_axis_bvhs[axis_id]`` — one BVHTree combining the
          world-space geometry of every object that belongs to that
          axis. The slider is checked against this single BVH per
          axis (rather than N individual object BVHs), so the axis
          participates in collision calculation as a single unit.
        - ``_obstacle_cache[obj_name]`` — per-object BVHTrees for any
          mesh in the scene that is NOT part of a registered axis
          (e.g. the artist's standalone ``Cube`` obstacle).

        Per-axis exclusion becomes implicit: a slider's own axis
        BVH is the one that's skipped in
        :meth:`check_slider_collision`, not individual object
        names.
        """
        self._rebuild_count += 1
        new_obstacle_cache: dict = {}
        new_axis_bvhs: dict = {}
        new_flat: set = set()

        # Refresh the flat union for any caller that still uses
        # the backward-compat path. The per-axis provider is the
        # live source of truth; the dict below is just a cache.
        if self._axis_names_provider is not None:
            try:
                new_flat = set(self._axis_names_provider())
            except Exception:
                new_flat = set()
        self._exclusion_cache = new_flat

        scene = self._get_live_scene()
        if scene is None or depsgraph is None:
            self._obstacle_cache = new_obstacle_cache
            self._axis_bvhs = new_axis_bvhs
            return
        try:
            scene_objects = list(getattr(scene, "objects", ()))
        except ReferenceError:
            self._scene = None
            self._obstacle_cache = new_obstacle_cache
            self._axis_bvhs = new_axis_bvhs
            return

        # Build a name → Object map for resolving axis membership
        # at rebuild time. The manager tracks the per-axis name
        # sets; here we just resolve them back to the live objects.
        name_to_obj: dict = {}
        for obj in scene_objects:
            n = getattr(obj, "name", None)
            if n:
                name_to_obj[n] = obj

        # Cache the axis_id -> host_obj mapping for ancestry checks
        # in ``check_slider_collision``. We rebuild this from the
        # manager's roster every time the cache is rebuilt, so it
        # stays in lockstep with the registered axes.
        new_axis_host_map: dict = {}
        if self._modules_by_id:
            # 拉模式:host map 直接来自本 tick 的模块名册,不再反向依赖
            # addon 全局单例。
            for _mid, _mod in self._modules_by_id.items():
                new_axis_host_map[_mid] = getattr(_mod, "host_obj", None)
        else:
            try:
                from ... import addon as _add
                _mgr = _add.get_manager()
            except Exception:
                _mgr = None
            if _mgr is not None:
                for _mod in _mgr.all():
                    _mid = getattr(_mod, "module_id", None)
                    if _mid is not None:
                        new_axis_host_map[_mid] = getattr(_mod, "host_obj", None)
        self._axis_host_map = new_axis_host_map

        # 1) Build per-axis combined BVHs. The per-axis provider
        # returns the set of Object names owned by each axis; we
        # resolve those names back to scene Objects and combine
        # their world-space geometry into one BVH per axis.
        # 组内对象名集合:拉模式走模块声明,否则退化到旧 provider。
        per_axis_names: dict = self._per_group_names()
        for axis_id, names in per_axis_names.items():
            objects = [
                name_to_obj[n] for n in names if n in name_to_obj
            ]
            objects = [o for o in objects if o is not None]
            bvh = _build_combined_bvh(objects, depsgraph)
            if bvh is not None:
                new_axis_bvhs[axis_id] = bvh

        # 2) Build per-object BVHs for non-axis obstacles. An
        # object is a non-axis obstacle if its name is not in any
        # per-axis name set (i.e. it's a standalone mesh the
        # artist added to the scene).
        all_axis_names: set = set()
        for names in per_axis_names.values():
            all_axis_names |= names
        # sensor mesh 不在 ``per_axis_names``(sensor 不进 per-axis
        # group BVH,参考 ``resolve_structure`` 的设计),但也不该被当作
        # 独立 obstacle 打进 ``_obstacle_cache`` —— 它是 trigger,不是
        # obstacle。这里从拉模式存下的 ``self._structures`` 中取出
        # 额外把 sensor 名字并入,让它们从 obstacle 列表里被踢除。
        for structure in self._structures.values():
            all_axis_names |= structure.sensor_names
        # ``exclusion_count`` / ``structure_snapshot()["group_names"]`` 读的就是
        # 它:拉模式(或有 per-axis provider 结果)下用**新鲜**的分组并集
        # 覆盖前面按 flat provider 取的旧值(旧值在 reparent 后不会失效);
        # 纯 ``attach_axis_names_provider`` 的旧式用例保持原值不变。
        if self._structures or all_axis_names:
            self._exclusion_cache = set(all_axis_names)
        for obj in scene_objects:
            if not is_obstacle_candidate(obj, set()):
                continue
            n = getattr(obj, "name", None)
            if n in all_axis_names:
                continue  # part of some axis; covered by the axis BVH
            tree = _build_bvh(obj, depsgraph)
            if tree is not None:
                new_obstacle_cache[obj.name] = tree

        self._axis_bvhs = new_axis_bvhs
        self._obstacle_cache = new_obstacle_cache

    # ---- per-axis query ----

    def _axes_are_related(self, axis_id_a: Optional[str], axis_id_b: Optional[str]) -> bool:
        """True iff ``axis_id_a``'s host and ``axis_id_b``'s host stand in
        an ancestor-descendant relationship in the scene tree.

        Used by :meth:`check_slider_collision` to suppress false
        positives when an artist parents one axis's body to
        another axis's body (e.g. a Y-axis mounted on an X-axis
        carrier). The two axes physically move together; an
        overlap between them is the rig bumping into its own
        payload, not a real collision that should stop the carrier.

        The check walks the parent chain of each axis's host and
        looks for the other axis's host as an ancestor. It's O(d)
        per call where ``d`` is the scene-tree depth — well under
        10 in practice.
        """
        return self._groups_are_related(axis_id_a, axis_id_b)

    def _groups_are_related(self, module_a, module_b) -> bool:
        """两个模块的组是否物理上挂在一起(host 祖孙关系)。

        集中式拉模式下的"相关组"判定:host map 来自本 tick 解析出的
        结构声明(``_structures``);没有结构声明时退回 ``_axis_host_map``
        (旧 provider 路径),保证旧测试与外部脚本行为不变。
        """
        if not module_a or not module_b:
            return False
        if module_a == module_b:
            return True
        if self._structures:
            return _groups_are_related_names(
                self._structures, module_a, module_b
            )
        host_a = self._axis_host_map.get(module_a)
        host_b = self._axis_host_map.get(module_b)
        if host_a is None or host_b is None:
            return False
        return _hosts_are_related(host_a, host_b)

    def check_slider_collision(self, slider_obj, depsgraph,
                              *, axis_id: Optional[str] = None) -> bool:
        """Return True iff ``slider_obj`` overlaps any obstacle BVH.

        The slider BVH is rebuilt on every call (sliders move every
        tick so their BVH must reflect the current world transform).
        Cost is O(n log n) for the slider; obstacle BVHs are reused
        from the cache.

        Per-axis exclusion: an obstacle whose name appears in the
        exclusion set for the **slider's** ``axis_id`` is skipped
        (it belongs to the same axis as the slider — internal
        collisions are not real hits). Obstacles belonging to other
        axes are kept; cross-axis collisions are valid.

        Ancestry exclusion: if the slider's axis and the other
        axis are in an ancestor-descendant relationship in the
        scene tree (e.g. a Y-axis host parented to the X-axis
        slider), the cross-axis check is suppressed. The two axes
        physically move together and an overlap between them is
        "the rig bumping its own payload", not a real collision.

        On a hit, the engine records the offending obstacle's name
        (and the optional ``axis_id``) into :attr:`last_collision` so
        the Dev panel can show "which axis hit which obstacle" — the
        most common false-positive case is a stale obstacle BVH from
        a moved mesh.

        The engine's own ``_enabled`` flag is **not** consulted here.
        Axis runtimes call this even while collision detection is
        disabled so the Dev panel can keep showing what *would* be hit;
        the disable toggle gates :meth:`mark_collision` / :meth:`read_marker`
        instead. Callers that must not lock an axis are expected to check
        :attr:`is_enabled` themselves — every axis runtime does.
        """
        if slider_obj is None or depsgraph is None:
            return False
        slider_bvh = _build_bvh(slider_obj, depsgraph)
        if slider_bvh is None:
            return False
        record = self._check_bvh_against_world(
            slider_bvh, axis_id, getattr(slider_obj, "name", None),
            depsgraph, body_obj=slider_obj,
        )
        if record is None:
            return False
        self._last_collision = record
        return True

    def _check_bvh_against_world(self, body_bvh, module_id,
                                 body_name, depsgraph=None, body_obj=None) -> Optional[dict]:
        """把一个运动体的世界 BVH 拿去比对"世界里其余一切"。

        跳过规则(实现见模块 docstring):

        - **同组**:``other_id == module_id`` → 跳过(组内不检测);
        - **相关组**:两个模块的 host 有祖孙关系 → 整对跳过(自动创建的
          组合结构 ``host=None``,**不**参与这条规则);
        - **其它组合**(不管是不是引擎自动包的)与独立障碍物 → 全检测。

        命中返回记录 ``dict``(不写 ``_last_collision``,由调用方决定)。
        ``kind``:

        - ``"group"`` —— 撞上另一个**模块声明**的组;
        - ``"structure"`` —— 撞上引擎**自动创建**的组合结构;
        - ``"obstacle"`` —— 撞上不属于任何组合的独立 mesh(仅兼容路径会出现)。
        """
        for other_id, group_bvh in self._axis_bvhs.items():
            if other_id == module_id:
                continue
            if self._groups_are_related(module_id, other_id):
                continue
            if bvh_overlap_exists(body_bvh, group_bvh):
                structure = self._structures.get(other_id)
                auto = bool(getattr(structure, "auto", False))
                overlapping = self._refine_overlap_members(
                    body_bvh, structure, depsgraph
                )
                # 2b:命中两侧都是刚体 → "刚体 vs 刚体"工艺接触,降级记录
                if self._is_rigid_pair(body_obj, overlapping):
                    return {
                        "axis_id": other_id,
                        "obstacle_name": overlapping[0] if overlapping else (
                            other_id[len(AUTO_ID_PREFIX):]
                            if other_id.startswith(AUTO_ID_PREFIX) else other_id
                        ),
                        "slider_name": body_name,
                        "body_name": body_name,
                        "kind": "rigid_contact",
                        "exempt": True,
                        "target_id": other_id,
                        "target_kind": "structure" if auto else "module",
                    }
                if auto:
                    display = (
                        other_id[len(AUTO_ID_PREFIX):]
                        if other_id.startswith(AUTO_ID_PREFIX) else other_id
                    )
                    precise = overlapping[0] if overlapping else None
                    record = {
                        "axis_id": other_id,
                        "obstacle_name": precise or display,
                        "slider_name": body_name,
                        "body_name": body_name,
                        "kind": "structure",
                        "target_id": other_id,
                        "target_kind": "structure",
                    }
                    if precise:
                        record["structure_name"] = display
                    return record
                return {
                    "axis_id": other_id,
                    "obstacle_name": f"axis:{other_id}",
                    "slider_name": body_name,
                    "body_name": body_name,
                    "kind": "group",
                    "target_id": other_id,
                    "target_kind": "module",
                }

        # 不属于任何组的独立障碍物(仅引擎未接管名册的兼容路径才可能非空)
        for name, obstacle_bvh in self._obstacle_cache.items():
            if bvh_overlap_exists(body_bvh, obstacle_bvh):
                record = {
                    "axis_id": module_id,
                    "obstacle_name": name,
                    "slider_name": body_name,
                    "body_name": body_name,
                    "kind": "obstacle",
                    "target_id": None,
                    "target_kind": "obstacle",
                }
                # 2b:障碍物是刚体且运动体也是刚体 → 同口径豁免
                if self._is_rigid_pair(body_obj, [name]):
                    record["kind"] = "rigid_contact"
                    record["exempt"] = True
                return record
        return None

    def _refine_structure_hit(self, body_bvh, structure, depsgraph):
        """自动组合命中后,在组内找出真正重叠的那个对象名。

        组合 BVH 是“整棵子树合并成一颗”,命中时只告诉你哪条组合被撞了。
        为了保留旧实现"哪个 mesh 被撞"的诊断精度,这里在**命中时**才把
        组内成员逐个建 BVH 比对(常态零开销,而且碰撞本身就是停机事件)。
        """
        if structure is None or depsgraph is None:
            return None
        names = self._refine_overlap_members(body_bvh, structure, depsgraph)
        return names[0] if names else None

    def _refine_overlap_members(self, body_bvh, structure, depsgraph) -> List[str]:
        """被撞组合里与 body_bvh 实际重叠的**全部**成员名(升序)。

        与 :meth:`_refine_structure_hit` 同一"命中时才逐个建 BVH"策略
        (常态零开销,碰撞本身就是停机事件)。2b 豁免判定需要全部重叠
        成员:只要有一个不是刚体,该命中就照常报警。
        """
        if structure is None or depsgraph is None:
            return []
        by_name: Dict[str, object] = {}
        for obj in self._scene_objects():
            name = _object_name(obj)
            if name is not None:
                by_name.setdefault(name, obj)
        out: List[str] = []
        for name in sorted(structure.group_names):
            member = by_name.get(name)
            if member is None:
                continue
            try:
                member_bvh = _build_bvh(member, depsgraph)
            except Exception:
                member_bvh = None
            if member_bvh is not None and bvh_overlap_exists(
                body_bvh, member_bvh
            ):
                out.append(name)
        return out

    def _is_rigid_pair(self, body_obj, member_names) -> bool:
        """2b 豁免判定:命中两侧都是刚体(带 ``rigid_body``)→ True。

        ``member_names`` 是被撞一侧实际重叠的成员名;**全部**是刚体且运动体
        本身也是刚体,才算"刚体 vs 刚体"的合法工艺接触(不写 marker、不
        BLOCKED)。成员解析不到(旧 provider 路径)→ 保守返回 False(照常报警)。
        """
        if not _is_rigid_body_object(body_obj):
            return False
        if not member_names:
            return False
        by_name: Dict[str, object] = {}
        for obj in self._scene_objects():
            name = _object_name(obj)
            if name is not None:
                by_name.setdefault(name, obj)
        for name in member_names:
            if not _is_rigid_body_object(by_name.get(name)):
                return False
        return True

    # ---- 集中式拉模式(engine 自己驱动全部碰撞计算) ----

    def _per_group_names(self) -> dict:
        """模块 → 组内对象名集合(``dict[module_id, set[str]]``)。

        优先级:本 tick 解析出的结构声明(拉模式)→ 旧 provider
        (``attach_per_axis_provider``)→ 空(退化即无边)。
        """
        if self._structures:
            return {
                mid: set(st.group_names)
                for mid, st in self._structures.items()
            }
        if self._per_axis_provider is None:
            return {}
        # 旧路径:provider 按 id 取值,所以需要模块名册。
        try:
            from ... import addon
            manager = addon.get_manager()
        except Exception:
            manager = None
        if manager is None:
            return {}
        out: dict = {}
        for module in manager.all():
            axis_id = getattr(module, "module_id", None)
            if axis_id is None:
                continue
            try:
                names = set(self._per_axis_provider(axis_id))
            except Exception:
                names = set()
            if names:
                out[axis_id] = names
        return out

    def _collect_structures(self) -> Dict[str, ResolvedStructure]:
        """向模块名册拉取 3D 结构声明,并展开为组内名字集。

        未实现 ``collision_structure()`` 的模块(含离线 mock)走
        :func:`collision_structure.legacy_structure` 字段名单 fallback,
        归类结果与重构前一致。
        """
        source = self._module_source
        if source is None:
            return {}
        try:
            modules = list(source())
        except Exception:
            return {}
        declared: Dict[str, CollisionStructure] = {}
        self._modules_by_id = {}
        for module in modules:
            module_id = getattr(module, "module_id", None)
            if module_id is None:
                continue
            self._modules_by_id[module_id] = module
            structure = None
            getter = getattr(module, "collision_structure", None)
            if getter is not None:
                try:
                    structure = getter()
                except Exception:
                    structure = None
            if structure is None:
                try:
                    structure = legacy_structure(module)
                except Exception:
                    structure = None
            if structure is None:
                continue
            if getattr(structure, "module_id", None) is None:
                structure = CollisionStructure(
                    module_id=module_id,
                    host=getattr(structure, "host", None),
                    members=tuple(getattr(structure, "members", ()) or ()),
                    bodies=tuple(getattr(structure, "bodies", ()) or ()),
                    sensors=tuple(getattr(structure, "sensors", ()) or ()),
                )
            declared[module_id] = structure
        host_names = {
            _object_name(st.host) for st in declared.values()
        }
        host_names.discard(None)
        resolved: Dict[str, ResolvedStructure] = {}
        for module_id, structure in declared.items():
            try:
                resolved[module_id] = resolve_structure(structure, host_names)
            except Exception:
                continue

        # ---- 场景树全量覆盖 ----
        # 未被任何声明认领的根节点 → 由引擎主动创建一个组合结构
        # (一个树节点 = 一个组合,内部同样不做碰撞检测)。
        # ``claimed`` 同时计入 ``group_names`` 与 ``sensor_names``:
        # sensor mesh 是 trigger 不是 obstacle,不会进任何 BVH,也不该被
        # build_auto_structures 包装成独立结构(否则它们会被当独立
        # obstacle,让跨轴 slider 走过 sensor 物理位置时被判 collision)。
        claimed: Set[str] = set()
        for structure in resolved.values():
            claimed |= structure.group_names
            claimed |= structure.sensor_names
        scene_objects = self._scene_objects()
        try:
            autos = build_auto_structures(
                scene_objects,
                claimed,
                host_names,
                is_candidate=lambda obj: is_obstacle_candidate(obj, set()),
            )
        except Exception:
            autos = []
        for auto in autos:
            resolved.setdefault(auto.module_id, auto)

        # 覆盖率自检:场景里每个候选 mesh 都必须落在某个组合里。
        # 这一步是“不能漏掉其他对象”的守门人 —— 不为空就说明有对象既没
        # 被模块声明、也没被自动组合包住。
        # sensor 已被声明认领在 ``sensor_names`` 字段里(不会进
        # ``group_names``,参考 :class:`CollisionStructure.sensors`),它
        # 是 trigger 不是 obstacle,依然算“覆盖”——把它也并入 covered,
        # 避免 :func:`structure_fingerprint` 把 sensor 报告为漏检
        # 噪声(sensor 不被包成 auto structure 也不会被当作独立障碍物,
        # 跳 warning 只是让日志干净)。
        self._uncovered = []
        covered: Set[str] = set()
        for structure in resolved.values():
            covered |= structure.group_names
            covered |= getattr(structure, "sensor_names", frozenset())
        for obj in scene_objects:
            name = _object_name(obj)
            if name is None or name in covered:
                continue
            if is_obstacle_candidate(obj, set()):
                self._uncovered.append(name)
        if self._uncovered:
            print(
                "[CollisionEngine] uncovered objects (no structure): %s"
                % ", ".join(sorted(self._uncovered)[:10])
            )
        return resolved

    def _scene_objects(self) -> list:
        """当前场景的物体列表(取不到返回空表)。"""
        scene = self._get_live_scene()
        if scene is None:
            return []
        try:
            return list(getattr(scene, "objects", ()) or ())
        except ReferenceError:
            self._scene = None
            return []
        except Exception:
            return []

    def _judge_bodies(self, depsgraph) -> Dict[str, dict]:
        """逐个模块、逐个运动体做重叠判定,返回命中记录。

        ``module.collision_bodies_active()`` 为 False 的模块本 tick 跳过
        (默认 True;保留 Cylinder "只在运动时检测"的旧语义)。
        """
        hits: Dict[str, dict] = {}
        for module_id, structure in self._structures.items():
            module = self._modules_by_id.get(module_id)
            checker = getattr(module, "collision_bodies_active", None)
            if checker is not None:
                try:
                    if not checker():
                        continue
                except Exception:
                    pass
            for body in structure.body_objects:
                if body is None:
                    continue
                body_bvh = _build_bvh(body, depsgraph)
                if body_bvh is None:
                    continue
                record = self._check_bvh_against_world(
                    body_bvh, module_id, _object_name(body), depsgraph,
                    body_obj=body,
                )
                if record is not None:
                    hits[module_id] = record
                    break
        return hits

    def _notify_hit(self, module_id: str, detail: dict,
                    *, empty: bool = False) -> None:
        """把本 tick 的判定结果告知模块(模块自己维护状态机)。"""
        module = self._modules_by_id.get(module_id)
        if module is None:
            return
        hook = getattr(module, "on_collision_hit", None)
        if hook is None:
            return
        try:
            hook(detail)
        except Exception:
            pass

    def step(self, depsgraph=None) -> Dict[str, dict]:
        """每 tick 一次:拉结构 → 比指纹 → 按需重建 → 全量判定 → 通知。

        由 :meth:`SimulationManager.update` 在所有 ``module.update(dt)``
        **之后**调用一次。关键顺序:模块本 tick 的 reparent / attach /
        release 已经发生,因此指纹比对能立即看到新层级 —— 这正是
        旧实现(固定 30 tick 重建)误判窗口的根除掉。

        返回本 tick 的 ``{module_id: detail}``。任一命中即写全局 scene
        marker(尊重 :attr:`is_enabled`),并立即通知肇事模块进 BLOCKED;
        其余模块通过 ``read_marker()`` 在下一个 tick 一起进 BLOCKED
        (与重构前语义一致)。

        重建时机:

        1. **结构指纹变化** → 立即重建(reparent / attach / 增删对象 /
           改父级 / 模块名册变化都在同一 tick 生效);
        2. **不属于任何组的独立障碍物被挪动** → 位姿进指纹,立即重建;
        3. 其余情况(运动体/组内成员在动) → 由
           ``rebuild_every_n_ticks`` 节奏兜底,与重构前一致(避免每
           tick 全量重建世界的 BVH)。
        """
        structures = self._collect_structures()
        if not structures:
            self._structures = {}
            self._modules_by_id = {}
            self._hits = {}
            return self._hits
        self._structures = structures
        objects = self._scene_objects()
        # 姿态跟踪(立即失效)只覆盖**自动组合结构**里的对象:它们是静基准
        # 件/装饰物,被挪动时必须马上生效;而模块声明组里的对象(运动体 +
        # 被它带着走的部件)每 tick 都在动,跟踪它们只会退化成每 tick 全量
        # 重建,那部分由重建节奏兜底。
        pose_tracked: Set[str] = set()
        for structure in structures.values():
            if getattr(structure, "auto", False):
                pose_tracked |= structure.group_names
        fingerprint = structure_fingerprint(objects, structures, pose_tracked)
        self._step_ticks += 1
        if (fingerprint != self._fingerprint
                or self._step_ticks >= self._rebuild_every_n_ticks):
            self._fingerprint = fingerprint
            self._step_ticks = 0
            self._rebuild_obstacle_cache(depsgraph)
        hits: Dict[str, dict] = {}
        if depsgraph is not None:
            hits = self._judge_bodies(depsgraph)
        self._hits = hits
        if hits:
            self._last_collision = next(iter(hits.values()))
        # 2b:kind="rigid_contact" 的"刚体 vs 刚体"工艺接触只记录、不报警
        real = {k: v for k, v in hits.items() if not v.get("exempt")}
        if real and self._enabled:
            self.mark_collision()
            for module_id, detail in real.items():
                self._notify_hit(module_id, detail)
        return hits

    def hit_for(self, module_id: str) -> Optional[dict]:
        """本 tick 该模块的命中详情(诊断用),无命中返回 ``None``。"""
        return self._hits.get(module_id)

    def is_blocked(self, module_id: str) -> bool:
        """本 tick 该模块是否肇事(不等同于全局 marker)。"""
        record = self._hits.get(module_id)
        return record is not None and not record.get("exempt")

    @property
    def rebuild_count(self) -> int:
        """BVH 缓存重建累计次数(指纹命中率测试/巡检钩子)。"""
        return self._rebuild_count

    def structure_snapshot(self) -> dict:
        """结构检视数据(Dev panel / RPC 诊断用)。

        返回::

            {
              "groups": {module_id: {"host", "objects", "bodies", "hit"}},
              "group_names": [...],   # 所有组声明的对象名并集
              "obstacles": [...],     # 被当作独立障碍物的对象名
              "last_collision": dict|None,
              "rebuild_count": int,
            }
        """
        groups: Dict[str, dict] = {}
        for module_id, structure in self._structures.items():
            groups[module_id] = {
                "host": structure.host_name,
                "objects": sorted(structure.group_names),
                "bodies": sorted(structure.body_names),
                "auto": bool(getattr(structure, "auto", False)),
                "hit": self._hits.get(module_id),
            }
        return {
            "groups": groups,
            "group_names": sorted(self._exclusion_cache),
            "obstacles": sorted(self._obstacle_cache.keys()),
            "uncovered": list(self._uncovered),
            "last_collision": self._last_collision,
            "rebuild_count": self._rebuild_count,
        }

    # ---- test hooks (intentional read-only views) ----

    @property
    def obstacle_count(self) -> int:
        """Number of non-axis obstacle BVHs currently cached. Test/inspection hook."""
        return len(self._obstacle_cache)

    @property
    def axis_bvh_count(self) -> int:
        """Number of per-axis combined BVHs currently cached. Test/inspection hook."""
        return len(self._axis_bvhs)

    @property
    def exclusion_count(self) -> int:
        """Number of axis-owned object names currently excluded. Test/inspection hook."""
        return len(self._exclusion_cache)


# ---- private bpy-dependent helpers (kept module-private) ----


def _build_bvh(obj, depsgraph):
    """Build a WORLD-space BVHTree for ``obj``.

    The previous implementation used :meth:`BVHTree.FromObject` which
    returns a BVH in **local** coordinates. Two mesh objects at
    different world positions both have their local-space geometry
    centred at the origin, so their local-space BVHs would always
    report overlap — the slider was "colliding" with every other
    object in the scene regardless of distance.

    Fix: extract the world-space vertex positions
    (``obj.matrix_world @ v.co``) and feed them to
    :meth:`BVHTree.FromPolygons` so the BVH is in the same
    world frame the runtime reasons about. The overlap check then
    reports only real geometric intersections.

    The depsgraph is still required so modifiers, shape keys, and
    constraints are reflected in the evaluated mesh.

    Triangulation
    -------------
    ``BVHTree.FromPolygons(all_triangles=True)`` used to accept
    arbitrary polygons in Blender 4.x. In Blender 5.x it raises
    ``ValueError: non triangle found`` for any non-triangle input.
    We fan-triangulate every N-gon here before constructing the
    BVH and pass the triangle list with ``all_triangles=False``.
    """
    try:
        eval_obj = obj.evaluated_get(depsgraph)
    except Exception:
        return None
    if eval_obj is None:
        return None
    try:
        from mathutils.bvhtree import BVHTree  # type: ignore
    except Exception:
        return None
    try:
        mesh = eval_obj.data
    except Exception:
        return None
    if mesh is None:
        return None
    if not hasattr(mesh, "vertices") or not hasattr(mesh, "polygons"):
        return None
    if len(mesh.polygons) == 0:
        return None
    try:
        mw = eval_obj.matrix_world
        verts = [tuple(mw @ v.co) for v in mesh.vertices]
        # 传 mesh 而非 mesh.polygons:让 _fan_triangulate 能检测带洞 face。
        tris = _fan_triangulate(mesh)
        return BVHTree.FromPolygons(
            verts, tris, all_triangles=False, epsilon=0.0
        )
    except Exception:
        return None


def _build_combined_bvh(objects, depsgraph):
    """Build a single world-space BVHTree combining the geometry of
    many Objects.

    Each object's mesh vertices are transformed by its
    ``matrix_world`` and concatenated; polygon indices are offset
    by the running vertex count so the combined BVH addresses
    vertices in the unified array. The result is one BVH that
    represents the entire logical group (e.g. an axis) as a
    single geometric entity, so the runtime can do
    ``slider_bvh.overlap(axis_bvh)`` instead of N per-object
    overlap checks.

    Returns ``None`` if no object contributes any geometry
    (degenerate group — e.g. every object is empty).
    """
    try:
        from mathutils.bvhtree import BVHTree  # type: ignore
    except Exception:
        return None
    all_verts: list = []
    all_polys: list = []
    for obj in objects:
        if obj is None:
            continue
        try:
            eval_obj = obj.evaluated_get(depsgraph)
        except Exception:
            continue
        if eval_obj is None:
            continue
        try:
            mesh = eval_obj.data
        except Exception:
            continue
        if mesh is None:
            continue
        if not hasattr(mesh, "vertices") or not hasattr(mesh, "polygons"):
            continue
        if len(mesh.polygons) == 0:
            continue
        try:
            mw = eval_obj.matrix_world
            base = len(all_verts)
            for v in mesh.vertices:
                all_verts.append(tuple(mw @ v.co))
            # 传 mesh 而非 mesh.polygons:让 _fan_triangulate 能检测带洞 face。
            for tri in _fan_triangulate(mesh):
                all_polys.append(tuple(i + base for i in tri))
        except Exception:
            continue
    if not all_polys:
        return None
    try:
        # ``all_triangles=False`` because we already fan-triangulated
        # every polygon in :func:`_fan_triangulate`. See the docstring
        # of :func:`_build_bvh` for why Blender 5.x requires this.
        return BVHTree.FromPolygons(
            all_verts, all_polys, all_triangles=False, epsilon=0.0
        )
    except Exception:
        return None


__all__ = [
    "SCENE_COLLISION_KEY",
    "STOP_REASON_COLLISION",
    "CollisionEngine",
    "CollisionStructure",
    "ResolvedStructure",
    "is_obstacle_candidate",
    "bvh_overlap_exists",
    "module_self_check",
    "current_depsgraph",
]