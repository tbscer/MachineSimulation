"""LinearAxis addon orchestrator (V0_3).

Lifecycle
---------
1. ``register()``:
   - Register the :class:`LinearAxisProperty` PropertyGroup on every
     ``bpy.types.Object`` (see :mod:`MotionSimulation.modules.LinearAxix.component`).
   - Construct the singleton :class:`SimulationManager` and start its
     tick (``bpy.app.timers`` callback, persistent).
   - Construct the singleton :class:`CollisionEngine`, wire it into
     the manager so the manager can answer
     ``all_axis_object_names()`` queries from the engine, and pass it
     into :func:`discover_and_register` so each axis runtime receives
     the engine through its constructor.
   - Construct the singleton :class:`MotionSimulation.rpc.RPCServer`,
     bind it to ``127.0.0.1:9877``, and start its accept thread + the
     main-thread state-push poller. The RPC server is a **local-only**
     JSON-over-TCP dispatcher: external clients send ``apply_command`` /
     ``subscribe_state`` / ``ping`` requests and receive state pushes.
   - Schedule :func:`_init_once` on a zero-delay one-shot timer to
     defer the first scene scan past Blender's ``_RestrictData`` window.

2. The zero-delay timer fires ``_init_once`` which calls
   :func:`discover_and_register`. This populates the manager with a
   passive :class:`LinearAxis` per ``LinearAxis*`` Empty host.

3. ``unregister()`` clears the manager, unregisters timers, stops the
   RPC server, and detaches the PropertyGroup.

Threading
---------
All ``bpy`` work runs on Blender's main thread. ``bpy`` data must only
be touched from the main thread, so :class:`SimulationManager._tick`
and :class:`rpc.RPCServer._push_tick` are both ``bpy.app.timers``
callbacks. The RPC server's worker threads handle socket I/O only;
every call into the manager (or any runtime) is marshalled onto the
main thread via :class:`rpc.server._MainThreadBridge`.
"""

from __future__ import annotations


TICK_HZ = 30
TICK_SECONDS = 1.0 / TICK_HZ


# Module-level singletons so :mod:`MotionSimulation.modules.LinearAxix.axis_ops`
# can find them without threading an instance through every call site.
_MANAGER: object | None = None
_COLLISION: object | None = None


def get_manager():
    """Return the singleton :class:`SimulationManager`, or ``None`` if
    the addon has not been registered yet.
    """
    return _MANAGER


def get_collision_engine():
    """Return the singleton :class:`CollisionEngine`, or ``None`` if
    the addon has not been registered yet.

    Exposed for MCP ``execute_code`` callers that want to clear the
    scene-level collision flag without going through a per-axis
    ``reset_collision`` command.
    """
    return _COLLISION


def _axis_names_provider():
    """Callable handed to :class:`CollisionEngine` for the flat-union
    exclusion set (backward-compat).

    Lives here (rather than inside the engine) so it can reach the
    manager singleton without a circular import: ``addon`` imports
    ``simulation_manager`` already, and the engine's provider is
    called lazily on every obstacle-cache rebuild.
    """
    if _MANAGER is None:
        return set()
    try:
        return _MANAGER.all_axis_object_names()
    except Exception:
        return set()


def _per_axis_exclusion_provider(axis_id: str) -> set:
    """Callable handed to :class:`CollisionEngine` for the per-axis
    exclusion set.

    Returns the Object names owned by the given ``axis_id`` so a
    slider of that axis is excluded from colliding with its own
    axis's objects (host, rail, shim, sensors, slider itself).
    Cross-axis collisions are NOT excluded.

    **走 fresh 路径, 不走 manager 缓存**: 缓存只在模块注册/反注册时
    失效, 但**运行时**美术在场景里改 parent 关系(把真空吸嘴
    parent 到 work_bar 这种)不会触发失效。走缓存会拿到过时的 axis
    物体名集合, collision engine 会把 axis 子物体错归为外部障碍,
    触发"撞自己"。见 :meth:`SimulationManager.axis_object_names_fresh`。
    """
    if _MANAGER is None:
        return set()
    try:
        return _MANAGER.axis_object_names_fresh(axis_id)
    except Exception:
        return set()


# ---- .blend 加载后重新 discovery ----
# 根因背景:host 对象上的 ``_*_discovered`` 标志是存进 .blend 的。插件在
# 启动时跑 ``_init_once`` 发现的是当时的场景;之后用户打开另一个 .blend,
# 没有任何入口重新 discovery —— 新文件里的盖章标志让 ``discover_and_register``
# 全部跳过,manager 名册为空(线上实测"模块注册数为 0"就是这么来的)。
# 这里挂一个 persistent 的 load_post 处理器:清掉旧模块残留(对象引用已失效,
# 不清也会被 ``SimulationManager.update`` 逐出)、清标志、全量重建、并作废
# 碰撞引擎的结构缓存。
try:
    from bpy.app.handlers import persistent as _persistent
except Exception:  # pragma: no cover —— 离线测试无 bpy
    def _persistent(fn):
        return fn


@_persistent
def _on_load_post(*_args) -> None:
    """``bpy.app.handlers.load_post``:新 .blend 加载完成后重新发现模块。"""
    import bpy
    from . import discovery

    scene = bpy.context.scene
    if scene is None or _MANAGER is None:
        return
    # 引擎还没构造(极早的 load_post,比 _init_once 定时器还早)→ 不要造出
    # 不带碰撞引擎的模块实例(register_module 对同 id 幂等丢弃,后注册的
    # 带引擎实例反而进不来)。退化成走 _init_once 的延迟初始化路径。
    if _COLLISION is None:
        if not bpy.app.timers.is_registered(_init_once):
            bpy.app.timers.register(_init_once, first_interval=0.0)
        return
    # 1. 丢掉旧文件残留的模块(它们的 Object 引用指向已卸载的 datablock)
    for module in list(_MANAGER.all()):
        mid = getattr(module, "module_id", getattr(module, "axis_id", None))
        if mid is None:
            continue
        try:
            _MANAGER.unregister_module(mid)
        except Exception as exc:
            print(f"[MotionSimulation] load_post unregister {mid} failed: {exc!r}")
    # 2. 清掉 .blend 里盖过章的 discovered 标志 → 全量重建
    try:
        discovery.clear_discovered_flags(scene)
    except Exception as exc:
        print(f"[MotionSimulation] load_post clear flags failed: {exc!r}")
    n = 0
    try:
        n = discovery.discover_and_register(
            scene, _MANAGER, collision_engine=_COLLISION
        )
    except Exception as exc:
        print(f"[MotionSimulation] load_post discovery failed: {exc!r}")
    # 3. 作废碰撞引擎的结构缓存(场景对象全换了)
    if _COLLISION is not None:
        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            _COLLISION.force_rebuild(depsgraph)
        except Exception as exc:
            print(f"[MotionSimulation] load_post force_rebuild failed: {exc!r}")
    print(f"[MotionSimulation] load_post rediscovered {n} module(s)")


def register() -> None:
    """Register the addon: PropertyGroup + manager + RPC + deferred init.

    Note: the :class:`CollisionEngine` is **not** constructed here.
    Its constructor needs ``bpy.context.scene``, which is unavailable
    inside the ``_RestrictContext`` that Blender activates during
    ``register()``. Engine construction is deferred to :func:`_init_once`.
    """
    import bpy
    from . import rpc, simulation_manager
    from .modules.LinearAxix import component, ui as linear_ui
    from .modules.RotateAxis import component as rotate_component, ui as rotate_ui
    from .modules.Cylinder import register as cylinder_register
    from .modules.components.approach_sensor import (
        register_rna as approach_sensor_register_rna,
        register_overlay as approach_sensor_register_overlay,
    )
    from .modules.components.vacuum_nozzle import (
        register_rna as vacuum_nozzle_rna_register,
        register_overlay as vacuum_nozzle_overlay_register,
    )
    from .modules.VacuumNozzle import register as vacuum_nozzle_register
    from .modules.ApproachSensor import register as approach_sensor_register
    from .modules.Conveyor import register as conveyor_register
    from .modules import dev_panel

    global _MANAGER
    # 1. PropertyGroups attached to every Object.
    component.register()
    rotate_component.register()
    linear_ui.register()
    rotate_ui.register()
    cylinder_register()
    # 1.4 ApproachSensor 所需的 Object RNA 属性 + 视口 overlay
    try:
        approach_sensor_register_rna()
    except Exception as exc:
        print(f"[MotionSimulation] approach sensor rna register failed: {exc}")
    # 1.45 VacuumNozzle PropertyGroup + RNA + 视口 overlay
    try:
        vacuum_nozzle_register()
    except Exception as exc:
        print(f"[MotionSimulation] vacuum nozzle register failed: {exc}")
    # 1.46 ApproachSensor module (纯被动感测器, 不产运动) PropertyGroup + UI
    # 从旧 Sensor 重命名而来 — Sensor 名称已被 LinearAxis 的 UTypeSensor
    # (sensor_direction / sensor_thickness / home_sensor / pos_sensor / neg_sensor)
    # 占用, 共享容易混淆, 改为 ApproachSensor 命名。
    try:
        approach_sensor_register()
    except Exception as exc:
        print(f"[MotionSimulation] approach sensor register failed: {exc}")
    # 1.47 Conveyor module(传送带, 不产自身运动, 驱动外部 rigid_body
    # 物体)PropertyGroup + UI
    try:
        conveyor_register()
    except Exception as exc:
        print(f"[MotionSimulation] conveyor register failed: {exc}")
    try:
        vacuum_nozzle_rna_register()
    except Exception as exc:
        print(f"[MotionSimulation] vacuum nozzle rna register failed: {exc}")
    dev_panel.register()
    # 1.5 传感器视口可视化（GPU overlay：局部坐标轴 + 触发盒）。
    # 独立注册，GPU 环境异常时不影响插件主体。
    try:
        from .modules.components.sensor import overlay as sensor_overlay
        sensor_overlay.register()
    except Exception as exc:
        print(f"[MotionSimulation] sensor overlay register failed: {exc}")
    # 1.55 Approach sensor 视口 overlay
    try:
        approach_sensor_register_overlay()
    except Exception as exc:
        print(f"[MotionSimulation] approach sensor overlay register failed: {exc}")
    # 1.56 VacuumNozzle 视口 overlay
    try:
        vacuum_nozzle_overlay_register()
    except Exception as exc:
        print(f"[MotionSimulation] vacuum nozzle overlay register failed: {exc}")
    # 1.6 RotateCenter 视口可视化（GPU overlay：局部轴 + 当前轴强调 + 十字标）。
    try:
        from .modules.RotateAxis import rotate_center_overlay
        rotate_center_overlay.register()
    except Exception as exc:
        print(f"[MotionSimulation] rotate center overlay register failed: {exc}")

    # 2. Singleton simulation manager. start() registers the tick.
    _MANAGER = simulation_manager.SimulationManager(frequency_hz=TICK_HZ)
    _MANAGER.start()

    # 2.5 Local RPC server. Started after the manager so the manager is
    # already alive to receive dispatched commands. The server is bound to
    # 127.0.0.1:9877 (configurable on the RPCServer class).
    rpc.register(_MANAGER)

    # 3. Defer the first scene scan + engine construction past
    # ``_RestrictData``. The timer fires ``_init_once`` once the
    # restricted context is gone and ``bpy.context.scene`` is safe
    # to read.
    if not bpy.app.timers.is_registered(_init_once):
        bpy.app.timers.register(_init_once, first_interval=0.0)

    # 4. .blend 加载后重新 discovery(见 _on_load_post docstring)。
    # persistent 标记保证换文件时 handler 不被 Blender 清掉。
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister() -> None:
    """Unregister the addon: cancel timers, clear manager, drop PropertyGroup."""
    import bpy
    from . import rpc
    from .modules.LinearAxix import component, ui as linear_ui
    from .modules.RotateAxis import component as rotate_component, ui as rotate_ui
    from .modules.Cylinder import unregister as cylinder_unregister
    from .modules.components.approach_sensor import (
        unregister_rna as approach_sensor_unregister_rna,
        unregister_overlay as approach_sensor_unregister_overlay,
    )
    from .modules.components.vacuum_nozzle import (
        unregister_rna as vacuum_nozzle_rna_unregister,
        unregister_overlay as vacuum_nozzle_overlay_unregister,
    )
    from .modules.VacuumNozzle import unregister as vacuum_nozzle_unregister
    from .modules.ApproachSensor import unregister as approach_sensor_unregister
    from .modules.Conveyor import unregister as conveyor_unregister
    from .modules import dev_panel

    global _MANAGER, _COLLISION
    if bpy.app.timers.is_registered(_init_once):
        bpy.app.timers.unregister(_init_once)
    try:
        while _on_load_post in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(_on_load_post)
    except Exception as exc:
        print(f"[MotionSimulation] load_post handler remove failed: {exc!r}")
    # Stop the RPC server *before* clearing the manager so no in-flight
    # dispatched command is applied to a half-torn-down manager.
    rpc.unregister()
    if _MANAGER is not None:
        _MANAGER.clear()
        _MANAGER = None
    _COLLISION = None
    dev_panel.unregister()
    try:
        approach_sensor_unregister_overlay()
    except Exception as exc:
        print(f"[MotionSimulation] approach sensor overlay unregister failed: {exc}")
    try:
        vacuum_nozzle_overlay_unregister()
    except Exception as exc:
        print(f"[MotionSimulation] vacuum nozzle overlay unregister failed: {exc}")
    try:
        approach_sensor_unregister_rna()
    except Exception as exc:
        print(f"[MotionSimulation] approach sensor rna unregister failed: {exc}")
    try:
        vacuum_nozzle_rna_unregister()
    except Exception as exc:
        print(f"[MotionSimulation] vacuum nozzle rna unregister failed: {exc}")
    try:
        vacuum_nozzle_unregister()
    except Exception as exc:
        print(f"[MotionSimulation] vacuum nozzle unregister failed: {exc}")
    try:
        approach_sensor_unregister()
    except Exception as exc:
        print(f"[MotionSimulation] approach sensor unregister failed: {exc}")
    try:
        conveyor_unregister()
    except Exception as exc:
        print(f"[MotionSimulation] conveyor unregister failed: {exc}")
    try:
        from .modules.components.sensor import overlay as sensor_overlay
        sensor_overlay.unregister()
    except Exception as exc:
        print(f"[MotionSimulation] sensor overlay unregister failed: {exc}")
    try:
        from .modules.RotateAxis import rotate_center_overlay
        rotate_center_overlay.unregister()
    except Exception as exc:
        print(f"[MotionSimulation] rotate center overlay unregister failed: {exc}")
    cylinder_unregister()
    rotate_ui.unregister()
    linear_ui.unregister()
    rotate_component.unregister()
    component.unregister()


def _init_once() -> None:
    """First-tick setup deferred past Blender's ``_RestrictData`` window.

    Touching ``bpy.data`` inside ``register()`` triggers the
    ``_RestrictData`` context guard (``bpy.context.scene`` raises
    ``AttributeError``). Running the engine construction + first
    scene scan on a zero-delay timer pushes the work past that guard.
    Returns ``None`` so the one-shot timer unregisters itself.
    """
    import bpy
    from . import discovery
    from .modules.components.collision import CollisionEngine

    global _COLLISION

    scene = bpy.context.scene
    if scene is None:
        return None  # retry on next tick

    # 0. 物理时钟对齐 (方案 A): Blender 刚体 dt = 1/scene.render.fps,
    #    而 manager 的 dt = 1/TICK_HZ。不对齐会让物理速度与模块逻辑
    #    速度不一致 (fps=24 而 tick=30 时, 每 tick 物理只前进 1/24s
    #    → 速度偏 ~25%)。
    try:
        if abs(float(scene.render.fps) - float(TICK_HZ)) > 0.01:
            print(
                f"[MotionSimulation] aligning scene fps "
                f"{scene.render.fps} -> {TICK_HZ} for physics clock"
            )
            scene.render.fps = int(TICK_HZ)
            scene.render.fps_base = 1.0
    except Exception as exc:
        print(f"[MotionSimulation] fps alignment failed: {exc!r}")

    # 0. Self-heal: drop ``_linear_axis_discovered`` / ``_rotate_axis_discovered``
    # flags stamped on host objects during any previous (possibly crashed)
    # session. Without this, ``discover_and_register`` would skip every
    # host that was ever registered before — the manager ends up with
    # ``_by_id == {}`` and RPC clients see ``AXIS_NOT_FOUND`` for every
    # id, even though the scene is fine. Clearing the flags makes the
    # following ``discover_and_register`` a full rebuild.
    try:
        discovery.clear_discovered_flags(scene)
    except Exception as exc:
        print(f"[MotionSimulation] clear_discovered_flags failed: {exc!r}")

    # 1. Construct the engine (safe to access bpy.context.scene here).
    if _COLLISION is None:
        _COLLISION = CollisionEngine(scene)
        # 集中式拉模式:engine 每 tick 自己向 manager 要模块名册,
        # 从每个模块的 ``collision_structure()`` 拿 3D 结构,
        # 自己做分组 / 缓存 / 判定 / 通知。
        _COLLISION.attach_module_source(_MANAGER.all)
        # 下面两个 provider 是旧路径(引擎未接管名册时用)的兼容入口,
        # 保留注入纯为向后兼容,拉模式下不再被使用。
        _COLLISION.attach_axis_names_provider(_axis_names_provider)
        _COLLISION.attach_per_axis_provider(_per_axis_exclusion_provider)
        _MANAGER.set_collision_engine(_COLLISION)

    # 2. Discovery — pass the engine so every axis runtime receives
    # it in its constructor. ``discover_and_register`` falls back to
    # ``manager.get_collision_engine()`` if ``collision_engine`` is
    # ``None``, so this is belt-and-braces.
    n = discovery.discover_and_register(scene, _MANAGER, collision_engine=_COLLISION)
    if _COLLISION is not None and n > 0:
        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
            _COLLISION.force_rebuild(depsgraph)
        except Exception:
            pass
    print(f"[MotionSimulation] discovered {n} module(s)")
    return None