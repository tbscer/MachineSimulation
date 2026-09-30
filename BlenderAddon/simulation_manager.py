"""SimulationManager: owns the simulation tick for all LinearAxes.

Modelled after
``Blender_DigitalTwin_LinearAxis_V0_2/Simulation/simulation_manager.py``
but **without** the threading. The reference framework's daemon
``threading.Thread`` is unsafe — every ``bpy`` read or write must
happen on Blender's main thread. This implementation uses
``bpy.app.timers`` so the tick stays on the main thread where bpy data
can be touched freely.

Lifecycle
---------
- Construction (``SimulationManager(frequency_hz=30.0)``) does not
  start ticking.
- :meth:`start` registers a persistent ``bpy.app.timers`` callback at
  ``first_interval=0.0`` (Blender's ``_RestrictData`` window passes
  before the first invocation).
- :meth:`stop` unregisters the callback.
- :meth:`clear` is ``stop`` + drop all axes; used at addon unregister.

Per-tick
--------
:meth:`_tick` calls :meth:`update` with ``self.dt`` and returns ``self.dt``
so ``bpy.app.timers`` re-arms at the requested cadence. :meth:`update`

1. ticks every registered module (stable snapshot; per-module
   exceptions are caught and printed so one bad module never kills
   the loop),
2. manages the rigid-body frame window (:meth:`_manage_frame_range`),
3. advances the scene frame by one (:meth:`_advance_scene_frame`),
4. runs the shared :class:`CollisionEngine`'s ``step()``.

物理时钟 (方案 A)
-----------------
Blender 的刚体 (Bullet) **只在帧变化时步进**, 而本 manager 跑在
``bpy.app.timers`` 上 —— 不推帧则物理永不运行。因此 :meth:`update`
在每个 tick 里调 ``scene.frame_set(frame + 1)``。实测结论:

* ``bpy.app.timers`` 回调里调 ``scene.frame_set`` 完全可用;
* Python 直写 ``obj.location`` + ``rigid_body.kinematic=True`` 即可产生
  摩擦 (不需要关键帧) —— 机器部件保持写 ``location`` 的风格;
* 帧号超过 ``rigidbody_world.point_cache.frame_end`` 后物理**冻结**,
  必须周期性 :meth:`_handoff_and_reset` (状态交接 + 帧复位);
* 场景对象集合变化 (增删 / reparent) 后必须 ``free_bake_all()``, 否则
  新对象不被模拟 —— 走 :meth:`notify_scene_changed`。

用户按 Play 时 Blender 自己推帧, 本 manager 会跳过推帧以避免双倍步进
(``_frame_advance_skip_playing``)。物理 dt 由 ``scene.render.fps`` 决定,
addon 启动时把它与 ``frequency_hz`` 对齐 (见 ``addon._init_once``)。

Collision engine integration
----------------------------
The manager exposes :meth:`all_axis_object_names` (used by
:class:`CollisionEngine` to exclude axis-owned meshes from the
obstacle scan) and :meth:`set_collision_engine` (called once at addon
register to wire the shared engine into the manager). The engine's
``step()`` is called from :meth:`update` **after** the module loop so it
sees the same tick's layer changes; the per-module ``collision_engine``
constructor argument is the legacy self-check path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

try:
    from .framework import BaseSimulationModule, SimulationCommand
except ImportError:  # pragma: no cover - offline / direct-script import path
    from framework import BaseSimulationModule, SimulationCommand

try:
    from .modules.components.collision_structure import LEGACY_MEMBER_FIELDS
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.components.collision_structure import (  # type: ignore
        LEGACY_MEMBER_FIELDS,
    )


# Object-name fields the manager introspects when collecting the set
# of axis-owned object names. ``LinearAxis`` (``slider``/``rail``/
# ``shim``), ``RotateAxisRuntime`` (``rotator``/``center``) and
# ``CylinderModule`` (``work_bar``/``touch_shim``/``approach_sensor_*``)
# are all covered so the per-axis exclusion list captures every object
# owned by a registered module.
#
# 这个 list 的语义是“任何 module 可能暴露的槽位名”，不是“每个 module
# 都有这些槽位”：``slider``/``rotator``/``work_bar`` 本就分属不同 kind，
# ``getattr(module, field, None)`` 对没有该槽位的 module 直接跳过。
#
# 漏掉 ``touch_shim`` / ``approach_sensor_*`` 的后果很严重：cylinder 的
# work_bar 会把自己的 touch_shim 与两个 approach sensor 当成“非轴障碍物”
# 建 BVH，而 touch_shim 按设计就贴在 work_bar 上 —— 一启动就“撞上自己”，
# 永远解不开。
_AXIS_OBJECT_FIELDS = LEGACY_MEMBER_FIELDS


def _walk_subtree(obj, visited: set, stop_names: Set[str] = frozenset()) -> Set[str]:
    """Return the set of Object names in the subtree rooted at ``obj``.

    Recursively walks ``obj`` and all its descendants (children,
    grandchildren, …) so the per-axis exclusion captures every
    mesh the artist has parented to the axis — not just the
    well-known slots enumerated in :data:`_AXIS_OBJECT_FIELDS`. The
    artist commonly adds extra helper meshes (e.g. a debug
    indicator parented to the slider, or a custom collision proxy
    parented to the rail) and the runtime should treat those as
    axis-internal too.

    ``visited`` is mutated in place so two branches that share a
    common descendant don't double-count it (and so a malformed
    parent-child cycle can't infinite-loop).

    ``stop_names`` is a set of object names whose subtrees are
    *not* descended into. When a node's name is in ``stop_names``
    we drop the entire subtree below it (and don't include the
    node itself) — not just the node's own name. The caller uses
    this to fence off a registered axis host from being claimed
    by a different axis whose body happens to sit higher up the
    scene tree; we must skip the whole child axis, not just its
    root.
    """
    if obj is None:
        return set()
    n = getattr(obj, "name", None)
    if n and n in stop_names:
        # Drop the whole subtree, but mark as visited so a later
        # shared-descendant walk won't re-enter and pick anything
        # up.
        visited.add(id(obj))
        return set()
    obj_id = id(obj)
    if obj_id in visited:
        return set()
    visited.add(obj_id)
    names: Set[str] = set()
    children = getattr(obj, "children", None) or ()
    if n:
        names.add(n)
    for child in children:
        names |= _walk_subtree(child, visited, stop_names)
    return names


def _collect_axis_object_names(module, stop_names: Set[str] = frozenset()) -> Set[str]:
    """Return the union of every Object name reachable from ``module``.

    Walks the standard slot names a module exposes (see
    :data:`_AXIS_OBJECT_FIELDS`: host, slider, rail, rotator, center,
    shim, work_bar, touch_shim, approach_sensor_1/2), plus every entry
    in its ``sensors`` list, AND the full child subtree of each of
    those objects. So a host that has a custom mesh parented as a
    direct child, or a slider that has a nested trigger shim
    grand-child, both end up in the exclusion set — the slider
    will not collide with them.

    Used by :class:`CollisionEngine` for per-axis exclusion (a
    slider does not collide with objects of its own axis, but
    CAN collide with objects of a different axis). Returns an
    empty set if the module exposes no recognisable fields
    (e.g. a test mock).

    ``stop_names`` is a set of object names whose subtrees must
    NOT be descended into. The caller passes in the names of all
    *other* registered axes' hosts so this axis stops claiming
    them — see :meth:`SimulationManager.register_module`. This
    keeps a slider's axis from accidentally owning a sibling
    axis's body that happens to live as a descendant in the
    scene tree.
    """
    names: Set[str] = set()
    visited: set = set()
    for field in _AXIS_OBJECT_FIELDS:
        holder = getattr(module, field, None)
        if holder is None:
            continue
        obj = getattr(holder, "obj", holder)
        names |= _walk_subtree(obj, visited, stop_names=stop_names)
    sensors = getattr(module, "sensors", None) or ()
    for s in sensors:
        obj = getattr(s, "obj", None)
        names |= _walk_subtree(obj, visited, stop_names=stop_names)
    return names


class SimulationManager:
    """Owns the simulation tick. Calls ``update(dt)`` on every registered module."""

    def __init__(self, frequency_hz: float = 30.0):
        if frequency_hz <= 0:
            raise ValueError("frequency_hz must be positive")
        self.frequency_hz = float(frequency_hz)
        self.dt = 1.0 / self.frequency_hz
        self._modules: List[BaseSimulationModule] = []
        self._by_id: dict = {}
        # Kind-keyed secondary index: ``_by_kind_id[(kind, module_id)]``.
        # Lets the per-module ``_require_axis`` disambiguate a
        # ``LinearAxis1`` from a ``RotateAxis1`` (both share
        # ``module_id == "1"`` by default). The flat ``_by_id`` index
        # is kept for backward-compatible ``get(module_id)`` callers;
        # its first match wins, which is fine when callers don't
        # need kind disambiguation (e.g. the per-axis update loop).
        self._by_kind_id: dict = {}
        # 兼容字段：Blender 5.1 的 register() 返回 None，所以它不代表“在跑”。
        # 判断 tick 是否运行请用 :meth:`is_running`。
        self._timer = None
        # tick 的回调对象必须 **缓存成同一个对象**：``bpy.app.timers`` 是按
        # **对象身份** 匹配回调的，而每次写 ``self._tick`` 都会新建一个 bound
        # method 对象。若每次都用新对象去 register/unregister/is_registered，
        # 就会出现：register 成功但 is_registered 恒为 False、
        # unregister 抛 "function is not registered"。
        self._tick_cb = self._tick
        # Per-axis object-name sets for the collision engine.
        # ``_axis_object_names_by_id[axis_id]`` is the set of Object
        # names belonging to a specific axis. The collision engine
        # uses this so a slider does not collide with objects of
        # its own axis, but CAN collide with objects of a different
        # axis (cross-axis collisions are valid hits).
        self._axis_object_names_by_id: dict = {}
        # Backward-compatible flat union of every axis object name.
        # Kept for any caller that just wants "is this an axis
        # object?" without per-axis scoping.
        self._axis_object_names: Set[str] = set()
        # Shared collision engine, installed by :meth:`set_collision_engine`
        # after the manager is constructed. Stored on the manager (rather
        # than imported as a module-global) so test harnesses can swap it
        # in without touching addon-level singletons.
        self._collision_engine = None

        # ---- 物理时钟 (方案 A) ------------------------------------
        # 详见类 docstring 的"物理时钟"节。
        # 默认 False = 纯逻辑模式(不推帧、Bullet 不步进)。位置计算驱动
        # 不需要物理时钟;要让 dynamic 刚体受重力/物理时再置 True。
        self._advance_frame: bool = False
        # 用户按 Play 时 Blender 自己推帧 → 这里跳过, 避免双倍步进。
        self._frame_advance_skip_playing: bool = True
        # 每次"状态交接"后重设的模拟窗口长度 (帧)。
        self._frame_window: int = 200
        # 距 point_cache.frame_end 还剩多少帧时触发交接。
        self._frame_headroom: int = 30
        # 场景对象集合变化 (增删 / reparent) → 下个 tick 清物理缓存。
        self._physics_dirty: bool = False

    # ---- registry ----

    def register_module(self, module: BaseSimulationModule) -> None:
        """Add a simulation module. Idempotent on ``module.module_id``.

        Modules are tracked in two indices:

        - ``_by_id`` flat dict for backward-compatible ``get(id)``
          lookups. Idempotent: a second registration with the same
          id is dropped.
        - ``_by_kind_id`` keyed by ``(kind, module_id)`` so a
          ``LinearAxis1`` and a ``RotateAxis1`` (both with
          ``module_id == "1"``) coexist. Disambiguates the per-axis
          ``send_command`` path that previously had no way to tell
          them apart.
        """
        module_id = getattr(module, "module_id", None)
        if module_id is None:
            raise ValueError("module must expose module_id")
        kind = getattr(module, "kind", None)
        # Kind-keyed index: (kind, module_id) is unique. Re-registering
        # the same combo is a no-op (so the same axis can be
        # ``refresh_axes``-ed without leaking duplicates).
        if kind is not None and (kind, module_id) in self._by_kind_id:
            return
        if module_id in self._by_id:
            # Already registered under a different kind — keep the
            # existing entry in the flat index (backward compat) and
            # add to the kind-keyed index so a per-kind lookup still
            # works.
            self._by_kind_id[(kind, module_id)] = module
            return
        self._modules.append(module)
        self._by_id[module_id] = module
        if kind is not None:
            self._by_kind_id[(kind, module_id)] = module
        # The cached owned-name set for this axis is invalidated
        # eagerly; the next ``axis_object_names(id)`` or
        # ``all_axis_object_names()`` call will recompute with the
        # full registered roster so the fence logic sees every
        # other axis's host. The previous implementation cached
        # only at register time, which left whichever axis
        # registered first (before its descendants) claiming the
        # descendants' subtree as its own.
        self._invalidate_owned_names()

    def register(self, axis) -> None:
        """Backward-compatible alias for registering legacy axis-like objects."""
        axis_id = getattr(axis, "axis_id", None)
        if axis_id is None:
            raise ValueError("axis must expose axis_id")
        if axis_id in self._by_id:
            return
        self._modules.append(axis)
        self._by_id[axis_id] = axis
        self._invalidate_owned_names()

    def unregister_module(self, module_id: str) -> bool:
        """Remove a module by id. Returns True if it was present."""
        module = self._by_id.pop(module_id, None)
        if module is None:
            return False
        try:
            self._modules.remove(module)
        except ValueError:
            pass
        # Invalidate owned-name caches — they will be recomputed
        # lazily on the next read, reflecting the smaller registered
        # roster.
        self._invalidate_owned_names()
        self._axis_object_names_by_id.pop(module_id, None)
        # Also drop from the kind-keyed index. The kind-name lookup
        # below is best-effort: not every module has a kind.
        kind = getattr(module, "kind", None)
        if kind is not None:
            self._by_kind_id.pop((kind, module_id), None)
        return True

    def unregister(self, axis_id: str) -> bool:
        """Backward-compatible alias for unregistering legacy axis-like objects."""
        return self.unregister_module(axis_id)

    def get_module(self, module_id: str):
        """Return the module with the given id, or ``None``.

        Returns the first match. When ``LinearAxis1`` and
        ``RotateAxis1`` are both registered (they share
        ``module_id == "1"``) this returns whichever was registered
        first. Use :meth:`get_module_by_kind` for unambiguous lookup.
        """
        return self._by_id.get(module_id)

    def get(self, axis_id: str):
        """Backward-compatible alias for axis lookup."""
        return self.get_module(axis_id)

    def get_module_by_kind(self, kind: str, module_id: str):
        """Return the module registered under ``(kind, module_id)``.

        Use this when the per-axis command path must disambiguate a
        collision on the bare ``module_id`` (e.g. a ``LinearAxis1``
        and a ``RotateAxis1`` both parse to id ``"1"``). Returns
        ``None`` if no module is registered under the given
        (kind, id) pair.
        """
        if kind is None:
            return self._by_id.get(module_id)
        return self._by_kind_id.get((kind, module_id))

    def _invalidate_owned_names(self) -> None:
        """Mark the cached owned-name sets stale; the next getter recomputes.

        Called by :meth:`register_module` and :meth:`register`
        whenever the registered roster changes. Cached values stay
        around for one extra lookup at worst; the next read goes
        through :meth:`_recompute_owned_names` and picks up every
        currently registered axis host as a fence boundary.
        """
        self._axis_object_names_by_id_dirty = True

    def _recompute_owned_names(self) -> None:
        """Walk every module's host subtree, applying the fence against
        every other axis's host_obj name.

        Populates ``_axis_object_names_by_id`` and the flat
        ``_axis_object_names`` union from scratch, then clears the
        dirty flag. Called by the read-side accessors
        (:meth:`axis_object_names`, :meth:`all_axis_object_names`)
        whenever the cached values are stale.
        """
        # Snapshot the host_obj of every registered module once so we
        # don't keep reaching through getattr inside the comprehension.
        all_hosts = {
            getattr(m, "host_obj", None)
            for m in self._modules
        }
        all_hosts.discard(None)
        all_host_names = {h.name for h in all_hosts}

        new_by_id: Dict[str, Set[str]] = {}
        flat: Set[str] = set()
        for m in self._modules:
            mid = getattr(m, "module_id", None)
            if mid is None:
                continue
            own_host = getattr(m, "host_obj", None)
            # Names to stop descending into: every other axis's host.
            stop = all_host_names - (
                {own_host.name} if own_host is not None else set()
            )
            new_by_id[mid] = _collect_axis_object_names(m, stop_names=stop)
            flat |= new_by_id[mid]

        self._axis_object_names_by_id = new_by_id
        self._axis_object_names = flat
        self._axis_object_names_by_id_dirty = False

    def axis_object_names(self, axis_id: str) -> Set[str]:
        """Return the set of Object names owned by a specific axis.

        Used by the collision engine for per-axis exclusion: a
        slider does not collide with objects of its own axis, but
        CAN collide with objects of a different axis. Returns an
        empty set if no axis is registered under ``axis_id``.

        Cached. The cache is invalidated by
        :meth:`register_module` / :meth:`unregister_module` and
        refreshed on the next call. **Not** invalidated by runtime
        scene-tree changes (parent reparenting, object rename,
        delete) — callers that need scene-fresh results must use
        :meth:`axis_object_names_fresh` instead.
        """
        if getattr(self, "_axis_object_names_by_id_dirty", False):
            self._recompute_owned_names()
        return set(self._axis_object_names_by_id.get(axis_id, ()))

    def axis_object_names_fresh(self, axis_id: str) -> Set[str]:
        """Return the set of Object names owned by ``axis_id``,
        **without consulting the cached owned-name set**.

        The cached path (:meth:`axis_object_names`) only refreshes
        when a module is registered or unregistered. **Reparenting**
        a mesh to or from an axis body — a common artwork
        operation, e.g. attaching a vacuum nozzle to a work bar —
        does **not** invalidate the cache. As a result, the cached
        set becomes stale: a freshly-parented child of the axis
        body is not in the set, the collision engine falls back to
        treating it as an external obstacle, and the work bar
        collides with its own child every tick.

        This method walks :func:`_collect_axis_object_names`
        directly so the result reflects the *current* scene tree.
        Cost is O(scene subtree depth) per call — well under
        microseconds for typical rigs. The collision engine calls
        this on every obstacle-cache rebuild (every
        ``rebuild_every_n_ticks`` ticks, default 30), so the
        aggregate cost is one walk per axis per ~1 s.

        Returns an empty set if no axis is registered under
        ``axis_id``.
        """
        module = self._by_id.get(axis_id)
        if module is None:
            return set()
        # 算 stop: 其他 axis 的 host name 集合
        all_hosts = {getattr(m, "host_obj", None) for m in self._modules}
        all_hosts.discard(None)
        all_host_names = {h.name for h in all_hosts}
        own_host = getattr(module, "host_obj", None)
        stop = all_host_names - (
            {own_host.name} if own_host is not None else set()
        )
        try:
            return set(_collect_axis_object_names(module, stop_names=stop))
        except Exception:
            return set()

    def all(self) -> list:
        """Return a snapshot list of every registered module."""
        return list(self._modules)

    def all_axis_object_names(self) -> Set[str]:
        """Return the union of every Object name owned by a registered module.

        The result is a fresh ``set`` copy so callers can mutate it
        freely (the collision engine does not, but defensive copy keeps
        the API safe).
        """
        if getattr(self, "_axis_object_names_by_id_dirty", False):
            self._recompute_owned_names()
        return set(self._axis_object_names)

    def set_collision_engine(self, engine) -> None:
        """Install the shared :class:`CollisionEngine` on the manager.

        Idempotent: a second call replaces the previous engine. The
        engine is stored by reference; the manager never calls it
        directly. Modules receive the engine through their constructor
        (``collision_engine=`` kwarg) so they can call it on every tick.
        """
        self._collision_engine = engine

    def get_collision_engine(self):
        """Return the installed :class:`CollisionEngine`, or ``None``."""
        return self._collision_engine

    def update(self, dt: float) -> None:
        """Advance every registered module by ``dt``, then step the physics clock.

        Modules that have flagged themselves as broken (``_alive ==
        False``) are unregistered rather than ticked — a stale Object
        reference will not resolve itself on a later tick and we don't
        want to spam the console every frame.

        执行顺序 (不可调换):

        1. 各 ``module.update(dt)`` —— 写机器部件 transform;
        2. :meth:`_manage_frame_range` —— 帧窗口管理 (必要时清缓存 / 交接);
        3. :meth:`_advance_scene_frame` —— 推一帧, 让 Bullet 步进;
        4. ``collision_engine.step()`` —— addon 自己的 BVH 判定。

        第 2/3 步见类 docstring 的"物理时钟"节。
        """
        for module in list(self._modules):
            if not getattr(module, "_alive", True):
                module_id = getattr(module, "module_id",
                                    getattr(module, "axis_id", "?"))
                kind = getattr(module, "kind", "module")
                print(f"[SimulationManager][{kind}][{module_id}] "
                      f"unregistering: stale datablock reference detected")
                self.unregister_module(module_id)
                continue
            try:
                module.update(dt)
            except Exception as exc:  # never let one bad module kill the loop
                module_id = getattr(module, "module_id", getattr(module, "axis_id", "?"))
                kind = getattr(module, "kind", "linear_axis")
                print(f"[{kind}][{module_id}] update failed: {exc!r}")

        # 物理时钟 (方案 A): 先管帧窗口, 再推一帧让 Bullet 步进。
        # 必须在模块循环**之后** —— Bullet 才能在本次步进中看到本 tick
        # 写入的 transform 变化, 并由此推导 kinematic 碰撞体的速度 (摩擦)。
        self._manage_frame_range()
        self._advance_scene_frame()

        # 集中式碰撞(拉模式):所有模块本 tick 的运动 / reparent /
        # attach / release 都做完之后,由 :class:`CollisionEngine` 自己
        # 拉取结构声明、比指纹、做全部重叠计算,并通知肇事模块。
        # 顺序很关键:放在模块循环之后,才能在同一 tick 内看到本 tick
        # 发生的层级变化(旧实现要等 30 tick 才对)。
        engine = self._collision_engine
        if engine is not None:
            step = getattr(engine, "step", None)
            if step is not None:
                try:
                    step(self._evaluated_depsgraph())
                except Exception as exc:  # 碰撞出错不允许杀死 tick
                    print(f"[SimulationManager][collision] step failed: {exc!r}")

    @staticmethod
    def _evaluated_depsgraph():
        """取 Blender evaluated depsgraph;离线 / 无 bpy 时返回 ``None``。"""
        try:
            import bpy
            return bpy.context.evaluated_depsgraph_get()
        except Exception:
            return None

    # ---- 物理时钟 (方案 A) ----

    @property
    def advance_frame(self) -> bool:
        """是否每 tick 推一帧 (物理时钟开关)。

        ``False``(默认) = 纯逻辑模式 (不推帧, 物理不步进) ——
        conveyor 回退位置计算后 (2024 会话决策) 一切运动都是位置写,
        不需要 Bullet 步进,时间线也不会被推着跑。
        ``True`` = 物理时钟模式 (每 tick 推帧让 Bullet 步进),仅在依赖
        dynamic 刚体的重力/接触仿真时开启。
        """
        return self._advance_frame

    @advance_frame.setter
    def advance_frame(self, value: bool) -> None:
        self._advance_frame = bool(value)

    def notify_scene_changed(self) -> None:
        """告知物理: 场景对象集合变了 (增删 / reparent), 需重建缓存。

        实测: 集合变化后若不 ``free_bake_all()``, 新对象不会被
        模拟 (永久冻结)。``VacuumNozzle`` 的 attach / release 走 reparent,
        必须调用本方法。
        """
        self._physics_dirty = True

    def _advance_scene_frame(self) -> None:
        """推一帧, 让 Blender 的刚体 (Bullet) 步进。

        为什么: Bullet 只在**帧变化**时步进, 而本 manager 跑在
        ``bpy.app.timers`` 上 —— 不推帧则物理永不运行。

        与用户播放互斥: 用户按 Play 时 Blender 自己推帧, 这里再推会
        双倍步进 → 跳过 (``_frame_advance_skip_playing``)。

        离线 / 无 bpy 环境 (pytest) 静默返回。
        """
        if not self._advance_frame:
            return
        try:
            import bpy
        except ImportError:  # 离线测试路径
            return
        try:
            scene = bpy.context.scene
        except Exception:
            return
        if scene is None:
            return
        if self._frame_advance_skip_playing:
            try:
                screen = bpy.context.screen
                if screen is not None and getattr(
                    screen, "is_animation_playing", False
                ):
                    return
            except Exception:
                pass
        try:
            scene.frame_set(scene.frame_current + 1)
        except Exception as exc:
            print(
                f"[SimulationManager][physics] frame advance failed: {exc!r}"
            )

    def _manage_frame_range(self) -> None:
        """保证当前帧之后仍有模拟区间; 处理缓存失效。

        实测: 帧号超过 ``rigidbody_world.point_cache.frame_end``
        后物理冻结, 且单纯扩大 ``frame_end`` 的补救不稳定 → 走
        :meth:`_handoff_and_reset` (状态交接 + 帧复位)。
        """
        if not self._advance_frame:
            return
        try:
            import bpy
            scene = bpy.context.scene
        except Exception:
            return
        if scene is None:
            return
        world = getattr(scene, "rigidbody_world", None)
        if world is None:
            self._physics_dirty = False
            return
        if self._physics_dirty:
            self._physics_dirty = False
            self._free_physics_cache()
            return
        cache = getattr(world, "point_cache", None)
        if cache is None:
            return
        try:
            near_end = (
                scene.frame_current + 1
                >= int(cache.frame_end) - self._frame_headroom
            )
        except Exception:
            near_end = False
        if near_end:
            self._handoff_and_reset()

    @staticmethod
    def _free_physics_cache() -> None:
        """清刚体点缓存 (幂等; 无 bpy / 无世界时静默跳过)。"""
        try:
            import bpy
            bpy.ops.ptcache.free_bake_all()
        except Exception:
            pass

    def _handoff_and_reset(self) -> None:
        """把 ACTIVE 刚体的物理状态写回 base transform, 清缓存, 帧复位。

        为什么: 帧号超过 ``point_cache.frame_end`` 后物理冻结。交接后帧号
        回到 ``frame_start``, 物理从**当前状态**继续 (不会弹回初始位置),
        缓存区间重新可用。

        只处理 ``ACTIVE`` 刚体 —— ``PASSIVE + kinematic`` 的机器部件
        (皮带 / work_bar) 由模块写 base transform, 物理不拥有它们,
        交接会破坏模块的运动学控制。
        """
        try:
            import bpy
            scene = bpy.context.scene
        except Exception:
            return
        if scene is None:
            return
        world = getattr(scene, "rigidbody_world", None)
        if world is None:
            return
        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
        except Exception:
            depsgraph = None
        collection = getattr(world, "collection", None)
        if collection is not None:
            for obj in collection.objects:
                rigid_body = getattr(obj, "rigid_body", None)
                if rigid_body is None or rigid_body.type != "ACTIVE":
                    continue
                try:
                    evaluated = (
                        obj.evaluated_get(depsgraph)
                        if depsgraph is not None else obj
                    )
                    matrix = evaluated.matrix_world
                    obj.location = matrix.to_translation()
                    obj.rotation_mode = "QUATERNION"
                    obj.rotation_quaternion = matrix.to_quaternion()
                except Exception:
                    continue
        self._free_physics_cache()
        try:
            scene.frame_set(scene.frame_start)
        except Exception:
            pass
        try:
            cache = getattr(world, "point_cache", None)
            if cache is not None:
                cache.frame_start = scene.frame_start
                cache.frame_end = scene.frame_start + self._frame_window
        except Exception:
            pass

    def apply_command(self, module_id: str, cmd: SimulationCommand) -> None:
        """Dispatch a command to a module if present."""
        module = self.get_module(module_id)
        if module is None:
            return
        module.apply_command(cmd)

    def snapshot(self) -> dict:
        """Return a state snapshot of all modules, grouped by ``category``.

        Returns ``{category: [snapshot, ...]}`` where ``category``
        comes from :attr:`BaseSimulationModule.category`. Modules
        sharing the same category (e.g. ``LinearAxis`` and
        ``RotateAxisRuntime`` both use ``"axes"``) appear under one
        top-level key as an ordered list of per-module snapshots, so
        RPC state_push consumers can iterate categories independently
        of dict-key ordering and each item is a self-contained record
        carrying its own ``module_id`` for correlation.

        Each snapshot is produced by ``module.snapshot()`` when present,
        else ``None`` — legacy ``register``'d modules that pre-date
        the snapshot contract show up as ``None`` rather than
        disappearing, so the category layout stays stable across module
        generations.

        Collision engine state
        -----------------------
        The shared :class:`CollisionEngine` is a **singleton per
        scene** (not a per-axis module), so it does not participate
        in the ``category -> list`` fan-out above. Instead, when an
        engine is installed via :meth:`set_collision_engine` we
        attach a dedicated ``"collision"`` key to the top-level
        dict. Its value is a flat dict (not a list — the engine has
        no per-axis replicas) shaped like::

            {
                "category": "collision",   # 方便客户端判别顶层 key 含义
                "enabled":  bool,         # engine.is_enabled
                "active":   bool,         # 任一轴被 BLOCKED；read_marker()
                "obstacle_count":   int,  # 已缓存的非轴障碍 BVH 数
                "axis_bvh_count":   int,  # 已缓存的 per-axis BVH 数
                "last_collision":   dict|None,  # 最近一次碰撞记录
            }

        当引擎尚未构造（``_collision_engine is None``，典型场景是
        addon 刚 ``register()`` 但 ``_init_once`` 还没跑）时，
        ``"collision"`` 字段仍会出现，但 ``enabled`` 与 ``active``
        都为 ``False``、``last_collision`` 为 ``None`` —— 客户端无
        需做存在性判断，可统一按 schema 处理。

        单引擎的设计也意味着 ``state_push`` 不会因为有 N 个轴就广播
        N 条 collision 记录：只有一条，集中反映当前场景的碰撞引擎状态。
        """
        grouped: Dict[str, list] = {}
        for module in self._modules:
            module_id = getattr(module, "module_id", None)
            if module_id is None:
                continue
            category = getattr(module, "category", None) or "_uncategorized"
            bucket = grouped.setdefault(category, [])
            bucket.append(
                module.snapshot() if hasattr(module, "snapshot") else None
            )
        # 顶层 ``collision`` 块：单例、与 axes category 并列。
        grouped["collision"] = self._collision_snapshot()
        return grouped

    def _collision_snapshot(self) -> dict:
        """Build the ``"collision"`` block for :meth:`snapshot`.

        Always returns a dict with the schema documented on
        :meth:`snapshot`. A missing engine still produces a
        well-formed record so RPC clients can iterate without
        ``if "collision" in data`` guards.
        """
        engine = self._collision_engine
        if engine is None:
            return {
                "category": "collision",
                "enabled": False,
                "active": False,
                "obstacle_count": 0,
                "axis_bvh_count": 0,
                "last_collision": None,
            }
        # The engine exposes ``is_enabled`` / ``read_marker`` /
        # ``obstacle_count`` / ``axis_bvh_count`` / ``last_collision``
        # — all defensive ``getattr`` so a custom engine (test
        # harness, legacy mock) that doesn't implement every helper
        # still yields a parseable dict instead of crashing the push.
        try:
            enabled = bool(getattr(engine, "is_enabled"))
        except Exception:
            enabled = False
        try:
            active = bool(getattr(engine, "read_marker")())
        except Exception:
            active = False
        try:
            obstacle_count = int(getattr(engine, "obstacle_count"))
        except Exception:
            obstacle_count = 0
        try:
            axis_bvh_count = int(getattr(engine, "axis_bvh_count"))
        except Exception:
            axis_bvh_count = 0
        try:
            last = getattr(engine, "last_collision")
            # ``last_collision`` 已经是 ``dict|None``；保险起见把里面的
            # ``None`` 字段也透传过去，方便客户端序列化。
            if last is not None and not isinstance(last, dict):
                last = {"raw": last}
        except Exception:
            last = None
        return {
            "category": "collision",
            "enabled": enabled,
            "active": active,
            "obstacle_count": obstacle_count,
            "axis_bvh_count": axis_bvh_count,
            "last_collision": last,
        }

    # ---- lifecycle ----

    def start(self) -> None:
        """Register the persistent tick with ``bpy.app.timers``.

        注意：Blender 5.1 的 ``bpy.app.timers.register()`` **返回 ``None``**（旧版
        返回一个不透明 handle），所以它的返回值不能当作“我是否在跑”的标志 ——
        之前用 ``self._timer is not None`` 判断，导致 ``_timer`` 恒为 ``None``、
        ``stop()`` 直接提前返回，tick 永远注销不掉。

        改用 ``bpy.app.timers.is_registered(self._tick_cb)`` 作为权威判断，
        见 :meth:`is_running`。注意必须传入 **缓存的那个** bound method 对象
        （``self._tick_cb``），因为 ``bpy.app.timers`` 按对象身份匹配回调。
        ``_timer`` 仅作为兼容字段保留，tick 运行期间它也可能仍是 ``None``。
        """
        import bpy
        if bpy.app.timers.is_registered(self._tick_cb):
            return
        self._timer = bpy.app.timers.register(
            self._tick_cb, first_interval=0.0, persistent=True
        )

    def is_running(self) -> bool:
        """True 当且仅当 tick 回调当前已注册（等价于“仿真在跑”）。

        用 ``bpy.app.timers.is_registered``（按对象身份匹配）查询缓存的
        ``self._tick_cb``。``bpy`` 不可用时（离线测试环境）返回 ``False``，
        这样 :meth:`stop` 在无 Blender 的环境下也能安全调用。
        """
        try:
            import bpy
        except ImportError:  # pragma: no cover - offline 路径
            return False
        return bpy.app.timers.is_registered(self._tick_cb)

    def stop(self) -> None:
        """注销 tick 回调（幂等）。"""
        if not self.is_running():
            self._timer = None
            return
        import bpy
        try:
            bpy.app.timers.unregister(self._tick_cb)
        except ValueError:
            # Not registered (already removed elsewhere).
            pass
        self._timer = None

    def clear(self) -> None:
        """Stop ticking and drop every module."""
        self.stop()
        self._modules.clear()
        self._by_id.clear()
        self._axis_object_names_by_id.clear()
        self._axis_object_names.clear()
        self._collision_engine = None

    # ---- tick ----

    def _tick(self) -> float:
        """Advance every registered module by ``self.dt``. Return ``self.dt`` to re-arm."""
        self.update(self.dt)
        return self.dt