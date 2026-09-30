"""Dev N-Panel: read-only status view of every discovered module.

Renders in the 3D-viewport N-panel under the ``MotionSimulation``
tab. Lists every module currently registered with the
:class:`SimulationManager` in four sub-panels — Linear Axes /
Rotate Axes / Cylinders / Vacuum Nozzles — and shows its live state: axes
read their ``axis_*`` custom-property snapshot, cylinders read
``CylinderModule.snapshot()`` (a cylinder has no slider / rotator
slot to hang ``axis_*`` props on), vacuum nozzles read
``VacuumNozzleModule.snapshot()``. Per-host controls (Home / Stop /
Move-To / Vacuum config) live in the per-host Object panel; this panel keeps the
global controls — **Refresh** (re-discover everything),
**Disable / Enable** collision detection, **Reset Flag** (clear the
scene-level collision flag) — plus a status readout for the
collision engine and a prominent *Clear collision* shortcut while
anything is blocked.

Collapsible axis sections
-------------------------
Each axis card has a chevron toggle in the header. The fold state
is stored on the scene as a per-axis dict key so it persists across
re-renders but is local to the current ``.blend`` file. With many
axes the operator can collapse the irrelevant ones to keep the
panel scannable. (The key is ``axis_id`` for axes and ``module_id``
for cylinders.)

Collision debug
---------------
False-positive collisions are the most common runtime surprise. The
panel surfaces the engine's ``last_collision`` record
(``axis_id`` / ``obstacle_name`` / ``slider_name``), the live scene
flag, the obstacle BVH count, and the exclusion-set size — the
information a developer needs to decide whether to clear the flag,
rebuild the cache, or fix the rig. The ``Reset Collision`` button
in the global controls row is the in-panel path for clearing the
flag without leaving the N-panel area; ``Disable`` is the kill
switch for "park the rig inside an obstacle and inspect the scene".
"""

from __future__ import annotations

import json

import bpy


# ---- 面板重绘辅助 ---------------------------------------------------------


def _redraw_view3d(context) -> None:
    """给所有 3D 视图区域打上重绘标记（刷新 N 面板内容）。

    Blender 不会在 operator 执行完后自动重绘 N 面板，必须主动 tag_redraw，
    否则 ENABLED / DISABLED 标签和每个轴的 State 都要等鼠标划过才更新。
    """
    try:
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    except Exception:
        pass


# 模块级延迟刷新句柄：保证同一时刻最多排一个延迟刷新 timer。
_PANEL_REFRESH: dict = {"timer": None}


def _schedule_panel_refresh(context, delay_s: float = 0.2) -> None:
    """约 ``delay_s`` 秒后再次重绘 3D 视图（一次性 timer）。

    轴运行时在 simulation tick 的末尾才把 axis_state / axis_stop_reason 等
    属性镜像到物体上；operator 里的立即重绘发生在这批属性更新之前，因此面板
    可能残留一两帧前的 "blocked"。延迟再刷一次让显示收敛到最终状态。
    """
    if _PANEL_REFRESH["timer"] is not None:
        return  # 已排过延迟刷新，避免重复排队。
    import bpy

    def _refresh_once() -> None:
        _PANEL_REFRESH["timer"] = None
        try:
            _redraw_view3d(context)
        except Exception:
            pass
        return None  # 一次性 timer：返回 None 即自动注销

    _PANEL_REFRESH["timer"] = bpy.app.timers.register(
        _refresh_once, first_interval=float(delay_s)
    )


# ---- operators ----------------------------------------------------------


class MS_OT_dev_refresh(bpy.types.Operator):
    """Drop every registered axis and re-run discovery."""

    bl_idname = "ms.dev_refresh"
    bl_label = "Refresh"
    bl_description = (
        "Drop every registered axis and re-run scene discovery. "
        "Use after renaming parts, reparenting, or adding new hosts."
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            from .LinearAxix.axis_ops import refresh_axes as lin_refresh
            from .RotateAxis.axis_ops import refresh_axes as rot_refresh
            n_lin = lin_refresh()
            n_rot = rot_refresh()
            self.report(
                {'INFO'},
                f"Refreshed: {n_lin} linear + {n_rot} rotate",
            )
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class MS_OT_dev_reset_collision(bpy.types.Operator):
    """Clear the scene-level collision flag and unblock all modules."""

    bl_idname = "ms.dev_reset_collision"
    bl_label = "Reset Collision"
    bl_description = (
        "Clear scene['motion_simulation_collision']; the next tick "
        "releases every blocked module (axes + cylinders)"
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        from .. import addon
        engine = addon.get_collision_engine()
        if engine is None:
            self.report({'ERROR'}, "Collision engine not initialised")
            return {'CANCELLED'}
        try:
            engine.clear_collision()
            # 立即重绘 N 面板，并延迟补刷一次（轴状态属性要到下一 tick 末尾
            # 才写回，第一次重绘时可能仍显示旧的 blocked）。
            _redraw_view3d(context)
            _schedule_panel_refresh(context)
            self.report({'INFO'}, "Scene collision flag cleared")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class MS_OT_dev_toggle_collision(bpy.types.Operator):
    """Disable or re-enable collision detection entirely.

    This is the artist-facing kill switch (see
    :meth:`CollisionEngine.set_disabled`). When the engine is disabled:

    - ``mark_collision`` becomes a no-op — even a real geometric
      overlap will not flip the scene-level flag, so modules won't
      re-enter STATE_BLOCKED on the next tick.
    - ``read_marker`` returns False, and every module currently in
      ``"blocked"`` (LinearAxis / RotateAxisRuntime / CylinderModule)
      is released **immediately** via ``_clear_blocked_state_only()``;
      the scene flag is cleared as part of the same call, so pressing
      :class:`MS_OT_dev_reset_collision` beforehand is no longer
      needed.
    - ``check_slider_collision`` still walks the BVHs and updates
      ``last_collision`` (so the Dev panel can show what *would*
      have hit), but the actual block is suppressed.

    Re-enabling clears the scene flag first too, so the rig never comes
    back already blocked.

    This is the artist-facing override for the common "the rig
    bumped its own payload, I just want to inspect the
    scene" case — the only way to get a stable, unblocked
    view from inside the Dev panel.
    """

    bl_idname = "ms.dev_toggle_collision"
    bl_label = "Toggle Collision"
    bl_description = (
        "Disable / re-enable collision detection. When disabled, "
        "scene['motion_simulation_collision'] is cleared and every "
        "blocked module (axes + cylinders) is released immediately."
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        from .. import addon
        engine = addon.get_collision_engine()
        if engine is None:
            self.report({'ERROR'}, "Collision engine not initialised")
            return {'CANCELLED'}
        try:
            # set_disabled 的参数是“是否禁用”。因此：当前开启 → 传 True
            # 表示禁用；当前禁用 → 传 False 表示重新启用。旧实现传的是
            # ``set_disabled(not engine.is_enabled)``，状态永远不会翻转，
            # 点击 Disable 没有任何效果、状态文字也与实际相反。
            disable_now = engine.is_enabled
            engine.set_disabled(disable_now)
            # 立即重绘 N 面板（切换后立刻看到新状态），并延迟补刷一次
            # （axis_* 属性在下一 tick 末尾才写回，避免残留 "blocked"）。
            _redraw_view3d(context)
            _schedule_panel_refresh(context)
            self.report(
                {'INFO'},
                "Collision detection DISABLED" if disable_now else
                "Collision detection ENABLED",
            )
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class MS_OT_dev_toggle_fold(bpy.types.Operator):
    """Toggle the per-axis fold state on the scene."""

    bl_idname = "ms.dev_toggle_fold"
    bl_label = "Toggle Fold"
    bl_description = "Expand / collapse the per-axis section in this panel"

    axis_id: bpy.props.StringProperty()

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        key = f"ms_dev_fold_{self.axis_id}"
        scene[key] = not bool(scene.get(key, False))
        return {'FINISHED'}


# ---- panels --------------------------------------------------------------


class MS_PT_dev(bpy.types.Panel):
    """Top-level N-panel tab. Holds the global controls + collision
    status. Per-axis lists live in sub-panels so the user can fold
    them and the inner content auto-scrolls when the list is long.
    """

    bl_label = "MotionSimulation Dev"
    bl_idname = "MS_PT_dev"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout

        from .. import addon
        engine = addon.get_collision_engine()

        # ---- global controls (Refresh + Reset Collision) ----
        top = layout.box()
        top.label(text="Global controls", icon="TOOL_SETTINGS")
        top.operator("ms.dev_refresh", icon="FILE_REFRESH")

        # ---- 传感器调试可视化开关（GPU overlay） ----
        scene = context.scene
        try:
            from ..modules.components.sensor.overlay import (
                SCENE_SHOW_KEY as _ov_show,
                SCENE_LIVE_KEY as _ov_live,
            )
            row = top.row(align=True)
            row.prop(scene, _ov_show, text="Sensor overlay", toggle=True)
            row.prop(scene, _ov_live, text="Live", toggle=True)
            top.label(
                text=(
                    "红/绿/蓝 = sensor 局部 X/Y/Z；白色 = 所选方向；"
                    "琥珀色盒 = 当前触发区"
                ),
                icon="INFO",
            )
        except Exception:
            pass

        # ---- 旋转中心调试可视化（GPU overlay） ----
        try:
            from ..modules.RotateAxis.rotate_center_overlay import (
                SCENE_SHOW_KEY as _rc_show,
                get_last_counts as _rc_counts,
                get_errors as _rc_errors,
                is_registered as _rc_is_reg,
            )
            row = top.row(align=True)
            row.prop(scene, _rc_show, text="Rotate center overlay", toggle=True)
            if not _rc_is_reg():
                top.label(
                    text=("RotateCenter overlay 未注册 —— 请 disable→enable 插件"),
                    icon="ERROR",
                )
            cc, hc = _rc_counts()
            top.label(
                text=(
                    f"RotateCenter overlay: {cc} mesh + {hc} host fallback drawn"
                ),
                icon=("CHECKMARK" if (cc + hc) > 0 else "INFO"),
            )
            top.label(
                text=(
                    "琥珀色十字 = 实际 RotateCenter（AABB 中心）；"
                    "紫色十字 = RotateAxis host 回退（center mesh 未建）"
                ),
                icon="INFO",
            )
            errs = _rc_errors()
            if errs:
                box = top.box()
                box.label(text="Overlay errors (latest):", icon="ERROR")
                for line in errs[-3:]:
                    box.label(text=line.splitlines()[0][:80])
        except Exception:
            pass

        if engine is not None:
            enabled = engine.is_enabled
            top.label(
                text=(
                    "Collision detection: ENABLED"
                    if enabled else
                    "Collision detection: DISABLED"
                ),
                icon=("CHECKMARK" if enabled else "CANCEL"),
            )
            # 当前是否有轴处于碰撞阻塞：读 scene 级 marker。引擎开启时，
            # 任一轴命中即置位并持续到 Reset / Disable；引擎被禁用时
            # read_marker 恒为 False，与“未检测到碰撞”的显示一致。
            active = engine.read_marker()
            top.label(
                text=(
                    "Collision state: ACTIVE — modules blocked"
                    if active else
                    "Collision state: clear"
                ),
                icon=("ERROR" if active else "CHECKMARK"),
            )
            # 引擎侧计数。最常用的排障方式：新加一个 Cube 之后看
            # ``Obstacles`` 有没有 +1 —— 没加就说明它还不在 BVH 缓存里
            # （缓存每 rebuild_every_n_ticks 重建一次，或手动 force_rebuild）。
            top.label(
                text=(
                    f"Obstacles: {engine.obstacle_count} BVH   "
                    f"excluded: {engine.exclusion_count} names"
                ),
                icon="INFO",
            )
            # 如果场景处于 BLOCKED，在面板顶部渲染一个醒目的“点这里清除”按钮。
            # 用户最常见的反馈是“Home 不会动” —— 实际是因为 BLOCKED
            # 吞掉了 home() 命令，必须先清 collision 才能恢复。
            if active:
                warn = top.box()
                warn.alert = True
                warn.label(
                    text="⚠ 全部模块 BLOCKED（轴 / cylinder），点这里先清除才能用 Home/Move-to",
                    icon="ERROR",
                )
                warn.operator(
                    "ms.dev_reset_collision",
                    text="Clear collision (release all axes)",
                    icon="X",
                )
            row = top.row(align=True)
            row.operator(
                "ms.dev_toggle_collision",
                text=("Disable" if enabled else "Enable"),
                icon=("PAUSE" if enabled else "PLAY"),
            )
            row.operator("ms.dev_reset_collision", text="Reset Flag", icon="X")

            last = engine.last_collision
            if last is not None:
                dbg = layout.box()
                dbg.label(text="Last collision record", icon="ERROR")
                dbg.label(text=f"  axis_id:    {last.get('axis_id')}")
                dbg.label(text=f"  obstacle:   {last.get('obstacle_name')}")
                dbg.label(text=f"  slider:     {last.get('slider_name')}")
                dbg.label(
                    text=(
                        "  Likely cause: a triangle of the obstacle is "
                        "inside the slider BVH. Move the obstacle, lower "
                        "its mesh resolution, or clear the scene flag "
                        "from MCP / RPC if the hit is transient."
                    ),
                    icon="INFO",
                )

            # ---- 碰撞结构检视(集中式拉模式)----
            # 每个 module 声明的"结构组" = host + 部件 + 声明的工件;
            # **组内永不做碰撞检测**。这里把分组结果与障碍物清单打出来:
            # 排查"明明撞了没报警 / 没撞却报警"时第一时间看这里。
            snapshot_fn = getattr(engine, "structure_snapshot", None)
            if snapshot_fn is not None:
                try:
                    snap = snapshot_fn()
                except Exception:
                    snap = None
                if snap:
                    struct_box = layout.box()
                    struct_box.label(text="Collision structure", icon="OUTLINER")
                    groups = snap.get("groups") or {}
                    module_ids = [
                        k for k in sorted(groups)
                        if not (groups[k] or {}).get("auto")
                    ]
                    auto_ids = [
                        k for k in sorted(groups)
                        if (groups[k] or {}).get("auto")
                    ]
                    if not groups:
                        struct_box.label(text="(no structure collected yet)")
                    # 模块声明的组先列,引擎主动创建的组合(未声明的根节点)
                    # 排在后面并带 [auto] 标记。
                    for module_id in module_ids + auto_ids:
                        info = groups[module_id] or {}
                        hit = info.get("hit")
                        is_auto = bool(info.get("auto"))
                        struct_box.label(
                            text="{}  host={}{}".format(
                                module_id, info.get("host"),
                                "   [auto]" if is_auto else "",
                            ),
                            icon=("ERROR" if hit else
                                  ("MESH_DATA" if is_auto else "GROUP")),
                        )
                        struct_box.label(
                            text="  group: "
                            + ", ".join(info.get("objects") or ["-"])
                        )
                        struct_box.label(
                            text="  bodies: "
                            + ", ".join(info.get("bodies") or ["-"])
                        )
                        if hit:
                            struct_box.label(
                                text="  HIT: {} [{}]".format(
                                    hit.get("obstacle_name"), hit.get("kind")
                                ),
                                icon="ERROR",
                            )
                    struct_box.label(
                        text="composites: {} module + {} auto".format(
                            len(module_ids), len(auto_ids)),
                        icon="GROUP",
                    )
                    uncovered = list(snap.get("uncovered") or [])
                    struct_box.label(
                        text=(
                            "uncovered mesh: none"
                            if not uncovered else
                            "uncovered mesh: " + ", ".join(uncovered[:6])
                        ),
                        icon=("CHECKMARK" if not uncovered else "ERROR"),
                    )
                    if snap.get("obstacles"):
                        # 拉模式下应永远为空(一切 mesh 都归某条组合)。
                        struct_box.label(
                            text="legacy obstacles: "
                            + ", ".join(snap.get("obstacles") or []),
                            icon="MESH_DATA",
                        )
                    struct_box.label(
                        text=(
                            f"BVH rebuilds: {snap.get('rebuild_count', '?')}"
                            "  (structure change rebuilds immediately)"
                        ),
                        icon="FILE_REFRESH",
                    )


# ---- shared render helpers (used by both sub-panels) ---------------------


def _get_modules_by_kind():
    """Return ``(linear_modules, rotate_modules, cylinder_modules, approach_sensor_modules, vacuum_modules, conveyor_modules)``.

    Cylinder 曾经被归到 ``linear`` 桶里，于是 Dev 面板的 “Linear Axes”
    列表会多出一个 ``?``（CylinderModule 没有 ``axis_id``）且状态全是
    ``<unknown>``（它也不写 ``axis_*`` 自定义属性）的条目。
    ApproachSensor module 同理（不产运动、不写 ``axis_*`` 属性）必须单列桶，
    否则会落到 "Linear Axes" 桶渲染出全 ``?`` 的空盒子。
    Conveyor module 也同构（category="axes" 与 LinearAxis 同桶，但 kind="conveyor"
    与 LinearAxis 不同，必须单列桶，否则 ``_draw_axis_body`` 会渲染出全
    ``<unknown>`` 的空盒子）。
    """
    from .. import addon
    manager = addon.get_manager()
    if manager is None:
        return [], [], [], [], [], []
    linear: list = []
    rotate: list = []
    cylinders: list = []
    approach_sensors: list = []
    vacuums: list = []
    conveyors: list = []
    for module in manager.all():
        kind = getattr(module, "kind", "linear_axis")
        if kind == "rotate_axis":
            rotate.append(module)
        elif kind == "cylinder":
            cylinders.append(module)
        elif kind == "approach_sensor":
            approach_sensors.append(module)
        elif kind == "vacuum_nozzle":
            # VacuumNozzle 没有 slider / rail / rotator，也不是 axis；
            # 不能归入 linear 桶，否则 ``_draw_axis_body`` 会渲染出一个
            # 全 ``?`` 的空盒子。
            vacuums.append(module)
        elif kind == "conveyor":
            # Conveyor 与 LinearAxis 共享 category="axes"，但 kind 不同；
            # 不能归入 linear 桶，否则 ``_draw_axis_body`` 会渲染出全
            # ``<unknown>`` 的空盒子。
            conveyors.append(module)
        else:
            linear.append(module)
    return linear, rotate, cylinders, approach_sensors, vacuums, conveyors


def _axis_snapshot(module) -> dict:
    kind = getattr(module, "kind", None)
    if kind == "rotate_axis":
        body_obj = getattr(getattr(module, "rotator", None), "obj", None)
    else:
        body_obj = getattr(getattr(module, "slider", None), "obj", None)
    if body_obj is None:
        return {}
    out: dict = {}
    for key in body_obj.keys():
        if not key.startswith("axis_"):
            continue
        val = body_obj[key]
        if key == "axis_sensors" and isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                pass
        out[key] = val
    return out


def _is_folded(axis_id: str) -> bool:
    return bool(bpy.context.scene.get(f"ms_dev_fold_{axis_id}", False))


def _scene_length_unit() -> str:
    """根据场景单位设置推断线性轴长度/速度的显示标签。

    插件内部所有标量都按 Blender 单位（BU）计算，不感知场景单位；
    这里只决定开发面板上给数值配什么标签。mm 工作流通常把
    Unit Scale 设为 0.001（1 BU = 1 mm），此时直接标 mm；否则回退到
    场景显示长度单位（米/厘米/mm），兜底 "m"。
    """
    try:
        us = bpy.context.scene.unit_settings
        scale = float(us.scale_length)
    except Exception:
        return "m"
    # 1 BU 不足 1 cm 的场景按 mm 工作流处理（0.001 最常见）。
    if scale <= 0.01:
        return "mm"
    return {
        "MILLIMETERS": "mm",
        "CENTIMETERS": "cm",
    }.get(getattr(us, "length_unit", "METERS"), "m")


def _draw_axis_header(layout, module, folded: bool) -> None:
    kind = getattr(module, "kind", "linear_axis")
    # CylinderModule 没有 ``axis_id``，只有 ``module_id``。
    axis_id = (getattr(module, "axis_id", None)
               or getattr(module, "module_id", "?"))
    host_name = getattr(getattr(module, "host_obj", None), "name", "?")

    row = layout.row(align=True)
    op = row.operator(
        "ms.dev_toggle_fold",
        text="",
        icon=("TRIA_DOWN" if folded else "TRIA_RIGHT"),
        emboss=False,
    )
    op.axis_id = axis_id

    icon = {
        "rotate_axis": "ORPHAN_DATA",
        "cylinder": "MESH_CYLINDER",
        "approach_sensor": "MESH_ICOSPHERE",
        "vacuum_nozzle": "MESH_ICOSPHERE",
    }.get(kind, "EMPTY_DATA")
    row.label(text=f"{kind}  {axis_id}", icon=icon)
    row.label(text=f"host={host_name}")


def _draw_cylinder_body(layout, module) -> None:
    """Cylinder 的运行状态行。

    CylinderModule 没有 slider / rotator 槽位，也不写 ``axis_*`` 自定义
    属性，所以不能走 :func:`_axis_snapshot`；直接用 ``module.snapshot()``
    （字段集见 ``modules/Cylinder/cylinder.py``）。
    """
    try:
        snap = module.snapshot()
    except Exception:
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    info = layout.column(align=True)
    info.label(text=f"State: {snap.get('state', '?')}")
    stop_reason = snap.get("stop_reason", "")
    if stop_reason:
        info.label(text=f"Stop reason: {stop_reason}")
    info.label(text=f"at: {snap.get('current_state', '?')}")
    info.label(
        text="sensors: A1={} A2={}".format(
            "1" if snap.get("approach_sensor_1") else "0",
            "1" if snap.get("approach_sensor_2") else "0",
        )
    )
    info.label(
        text="outputs: O1={} O2={}".format(
            "1" if snap.get("output_1") else "0",
            "1" if snap.get("output_2") else "0",
        )
    )


def _draw_vacuum_body(layout, module) -> None:
    """VacuumNozzle 的运行状态行。

    与 Cylinder 同理:VacuumNozzleModule 没有 slider / rotator 槽位,
    也不写 ``axis_*`` 自定义属性,所以不能走 :func:`_axis_snapshot`;
    直接用 ``module.snapshot()``(字段集见
    ``modules/VacuumNozzle/runtime.py``)。
    """
    try:
        snap = module.snapshot()
    except Exception:
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    info = layout.column(align=True)
    info.label(text=f"State: {snap.get('state', '?')}")
    stop_reason = snap.get("stop_reason", "")
    if stop_reason:
        info.label(text=f"Stop reason: {stop_reason}")
    info.label(
        text="on: {}   sensing: {}   holding: {} ({})".format(
            "1" if snap.get("enabled") else "0",
            "1" if snap.get("sensing") else "0",
            "1" if snap.get("holding") else "0",
            snap.get("held_count", 0),
        )
    )
    held_names = snap.get("held_names") or []
    if held_names:
        info.label(text="picked: " + ", ".join(str(n) for n in held_names))
    info.label(text=f"anchor: {snap.get('anchor_name')}")


def _draw_approach_sensor_body(layout, module) -> None:
    """ApproachSensor module 的运行状态行。

    ApproachSensorModule 不写 ``axis_*`` 自定义属性，也不产运动 —— 直接用
    ``module.snapshot()``（字段集见 ``modules/ApproachSensor/runtime.py``）。
    """
    try:
        snap = module.snapshot()
    except Exception:
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    info = layout.column(align=True)
    info.label(text=f"State: {snap.get('state', '?')}")
    stop_reason = snap.get("stop_reason", "") or ""
    if stop_reason:
        info.label(text=f"Stop reason: {stop_reason}")
    triggered = bool(snap.get("is_triggered", False))
    info.label(
        text="triggered: {} ({})".format(
            "1" if triggered else "0",
            snap.get("triggered_obj_name", "") or "(none)",
        ),
        icon=("RADIOBUT_ON" if triggered else "RADIOBUT_OFF"),
    )
    cube = snap.get("cube_size") or (0.0, 0.0, 0.0)
    info.label(
        text="cube=(l={:.2f}, w={:.2f}, h={:.2f})".format(
            float(cube[0]) if len(cube) > 0 else 0.0,
            float(cube[1]) if len(cube) > 1 else 0.0,
            float(cube[2]) if len(cube) > 2 else 0.0,
        )
    )
    center = snap.get("working_face_center") or (0.0, 0.0, 0.0)
    info.label(
        text="center=({:.2f}, {:.2f}, {:.2f})".format(
            float(center[0]) if len(center) > 0 else 0.0,
            float(center[1]) if len(center) > 1 else 0.0,
            float(center[2]) if len(center) > 2 else 0.0,
        )
    )


def _draw_conveyor_body(layout, module) -> None:
    """Conveyor 的运行状态行。

    Conveyor 不写 ``axis_*`` 自定义属性，也不产自身运动 —— 直接用
    ``module.snapshot()``(字段集见 ``modules/Conveyor/runtime.py``)。
    """
    try:
        snap = module.snapshot()
    except Exception:
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    info = layout.column(align=True)
    info.label(text=f"State: {snap.get('state', '?')}")
    stop_reason = snap.get('stop_reason', '')
    if stop_reason:
        info.label(text=f"Stop reason: {stop_reason}")
    info.label(
        text="running: {}   direction: {}".format(
            "1" if snap.get('running') else "0",
            "forward" if snap.get('direction', 0) == 1 else "reverse",
        )
    )
    info.label(
        text="speed: {:.3f} mm/s   friction: {:.3f}".format(
            float(snap.get('target_speed', 0.0)),
            float(snap.get('friction', 0.0)),
        )
    )
    driven_names = snap.get('driven_names') or []
    # uv_offset / driven_count 已从 state_push snapshot 移除(带宽考虑),
    # Dev panel 直接读实例内部值,显示不变。
    info.label(
        text="uv_offset: {:.4f}   driven_count: {}".format(
            float(getattr(module, "_uv_offset", 0.0)),
            len(driven_names),
        )
    )
    if driven_names:
        info.label(text="driven: " + ", ".join(str(n) for n in driven_names))


def _draw_axis_body(layout, module) -> None:
    kind = getattr(module, "kind", "linear_axis")
    if kind == "cylinder":
        _draw_cylinder_body(layout, module)
        return
    if kind == "approach_sensor":
        _draw_approach_sensor_body(layout, module)
        return
    if kind == "vacuum_nozzle":
        _draw_vacuum_body(layout, module)
        return
    if kind == "conveyor":
        _draw_conveyor_body(layout, module)
        return
    snap = _axis_snapshot(module)
    state = snap.get("axis_state", "<unknown>")
    stop_reason = snap.get("axis_stop_reason", "")
    if kind == "rotate_axis":
        pos_label = f"angle={snap.get('axis_current_angle', '?')}"
        tgt_label = f"target_angle={snap.get('axis_target_angle', '?')}"
        unit = "deg"
    else:
        pos_label = f"x={snap.get('axis_current_x', '?')}"
        tgt_label = f"target_x={snap.get('axis_target_x', '?')}"
        unit = _scene_length_unit()

    info = layout.column(align=True)
    info.label(text=f"State: {state}")
    if stop_reason:
        info.label(text=f"Stop reason: {stop_reason}")
    info.label(text=pos_label)
    info.label(text=tgt_label)
    info.label(text=f"velocity={snap.get('axis_velocity', '?')} {unit}/s")
    if snap.get("axis_has_target"):
        info.label(text=tgt_label + " [has target]")

    sensors = snap.get("axis_sensors") or {}
    if isinstance(sensors, dict) and sensors:
        triggered = [n for n, v in sensors.items() if v]
        if triggered:
            info.label(
                text=f"sensors ON: {', '.join(triggered)}",
                icon="RADIOBUT_ON",
            )


def _draw_axis_box(layout, module) -> None:
    axis_id = (getattr(module, "axis_id", None)
               or getattr(module, "module_id", "?"))
    box = layout.box()
    folded = _is_folded(axis_id)
    _draw_axis_header(box, module, folded)
    if folded:
        return
    _draw_axis_body(box, module)


# ---- sub-panels for the linear / rotate axis lists ----------------------


class MS_PT_dev_linear(bpy.types.Panel):
    """Sub-panel listing every registered LinearAxis.

    A sub-panel (vs. a plain ``box()``) gives two things for free:

    - **Fold / unfold** of the entire list (the header chevron),
      independent of the parent panel.
    - **Vertical scroll bar** when the list overflows the N-panel
      height — Blender adds it automatically because the sub-panel
      content extends past the available viewport.
    """

    bl_label = "Linear Axes"
    bl_idname = "MS_PT_dev_linear"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_parent_id = "MS_PT_dev"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        linear, _, _, _, _, _ = _get_modules_by_kind()
        layout = self.layout
        if not linear:
            layout.label(text="(none registered)")
            return
        for module in linear:
            _draw_axis_box(layout, module)


class MS_PT_dev_rotate(bpy.types.Panel):
    """Sub-panel listing every registered RotateAxis. See
    :class:`MS_PT_dev_linear` for why the sub-panel pattern is used."""

    bl_label = "Rotate Axes"
    bl_idname = "MS_PT_dev_rotate"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_parent_id = "MS_PT_dev"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        _, rotate, _, _, _, _ = _get_modules_by_kind()
        layout = self.layout
        if not rotate:
            layout.label(text="(none registered)")
            return
        for module in rotate:
            _draw_axis_box(layout, module)


class MS_PT_dev_cylinder(bpy.types.Panel):
    """Sub-panel listing every registered CylinderModule.

    Cylinder 不是 axis（没有 slider / rail，运动体叫 ``work_bar``），但
    它共用同一套 collision 契约（同一个单例 engine + 同一个 scene
    marker），所以状态显示放在同一个 Dev 面板里：``state == "blocked"``
    时这里是和上面那个 ``Clear collision`` 按钮配套的唯一可读入口。

    See :class:`MS_PT_dev_linear` for why the sub-panel pattern is used.
    """

    bl_label = "Cylinders"
    bl_idname = "MS_PT_dev_cylinder"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_parent_id = "MS_PT_dev"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        _, _, cylinders, _, _, _ = _get_modules_by_kind()
        layout = self.layout
        if not cylinders:
            layout.label(text="(none registered)")
            return
        for module in cylinders:
            _draw_axis_box(layout, module)


class MS_PT_dev_approach_sensor(bpy.types.Panel):
    """子面板列出每一个注册的 ApproachSensorModule。

    ApproachSensor 不产运动也不写 ``axis_*`` 属性，所以状态显示走
    ``_draw_approach_sensor_body`` 直接读 ``module.snapshot()``。
    与 VacuumNozzle / Cylinder 同理：共用同一套 collision 契约，
    ``state == "blocked"`` 时这里是确认“撞了没”的入口。
    See :class:`MS_PT_dev_linear` for why the sub-panel pattern is used.
    """

    bl_label = "Approach Sensors"
    bl_idname = "MS_PT_dev_approach_sensor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_parent_id = "MS_PT_dev"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        _, _, _, approach_sensors, _, _ = _get_modules_by_kind()
        layout = self.layout
        if not approach_sensors:
            layout.label(text="(none registered)")
            return
        for module in approach_sensors:
            _draw_axis_box(layout, module)


class MS_PT_dev_vacuum(bpy.types.Panel):
    """Sub-panel listing every registered VacuumNozzleModule.

    VacuumNozzle 既不产生运动也不写 ``axis_*`` 属性,只做「开真空吸住 /
    关真空丢掉」的父子切换;它的配置(``enabled`` + 感应区几何)在
    **选中 host 的 Object Properties 面板**里改。
    这里只做状态回显 —— 和 Cylinder 一样共用同一套 collision 契约,
    ``state == "blocked"`` 时是确认“撞了没”的入口。

    See :class:`MS_PT_dev_linear` for why the sub-panel pattern is used.
    """

    bl_label = "Vacuum Nozzles"
    bl_idname = "MS_PT_dev_vacuum"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_parent_id = "MS_PT_dev"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        _, _, _, _, vacuums, _ = _get_modules_by_kind()
        layout = self.layout
        if not vacuums:
            layout.label(text="(none registered)")
            return
        for module in vacuums:
            _draw_axis_box(layout, module)


class MS_PT_dev_conveyor(bpy.types.Panel):
    """子面板列出每一个注册的 ConveyorModule。

    Conveyor 与 LinearAxis 共享 ``category="axes"`` 但 kind 不同 —— 在 Dev 面板
    里单列。与 Cylinder / VacuumNozzle / Sensor 同理：共用同一套 collision 契约，
    ``state == "blocked"`` 时这里是确认“撞了没”的入口。
    See :class:`MS_PT_dev_linear` for why the sub-panel pattern is used.
    """

    bl_label = "Conveyors"
    bl_idname = "MS_PT_dev_conveyor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MotionSimulation"
    bl_parent_id = "MS_PT_dev"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from .. import addon
        return addon.get_manager() is not None

    def draw(self, context: bpy.types.Context) -> None:
        _, _, _, _, _, conveyors = _get_modules_by_kind()
        layout = self.layout
        if not conveyors:
            layout.label(text="(none registered)")
            return
        for module in conveyors:
            _draw_axis_box(layout, module)


# ---- registration --------------------------------------------------------


_CLASSES = (
    MS_OT_dev_refresh,
    MS_OT_dev_reset_collision,
    MS_OT_dev_toggle_collision,
    MS_OT_dev_toggle_fold,
    MS_PT_dev,
    MS_PT_dev_linear,
    MS_PT_dev_rotate,
    MS_PT_dev_cylinder,
    MS_PT_dev_approach_sensor,
    MS_PT_dev_vacuum,
    MS_PT_dev_conveyor,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass