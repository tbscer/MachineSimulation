# -*- coding: utf-8 -*-
"""Conveyor host 的 Object Properties 面板。

在 PROPERTIES > object context 下,选中物体是 ``Conveyor*`` host 时显示。

字段分组:
- **Host refs** —— drive_roller / idler_roller / belt
- **Motion** —— target_speed (mm/s) / direction (forward/reverse) /
  friction (0..1)
- **Control** —— running toggle + Refresh + Apply
- **Runtime state** —— state / running / direction / target_speed /
  friction / uv_offset / driven_count
"""

from __future__ import annotations

import bpy


def _has_prop(pg, name: str) -> bool:
    """PropertyGroup 字段存在性检查(容错 deferred RNA)。"""
    try:
        return name in type(pg).bl_rna.properties
    except Exception:
        return hasattr(pg, name)


class CONVEYOR_OT_refresh(bpy.types.Operator):
    """Re-run scene discovery;让 conveyor host 的 pointer 改动立刻生效。"""

    bl_idname = "conveyor.refresh"
    bl_label = "Refresh Conveyor"
    bl_description = "Re-run scene discovery and rebuild Conveyor host references"

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            from ... import discovery as _top_discovery
            from ...addon import get_manager
            scene = context.scene
            mgr = get_manager()
            if scene is not None and mgr is not None:
                _top_discovery.clear_discovered_flags(scene)
                n = _top_discovery.discover_and_register(scene, mgr)
                self.report({'INFO'}, f"Discovered {n} module(s)")
            else:
                self.report({'ERROR'}, "scene or manager not ready")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class CONVEYOR_OT_apply(bpy.types.Operator):
    """便捷按钮:把当前 panel 的 cfg 字段(target_speed / direction /
    friction / running)推回 instance。"""

    bl_idname = "conveyor.apply"
    bl_label = "Apply"
    bl_description = (
        "把当前面板上的 target_speed / direction / friction / running 写回"
        "ConveyorProperty(通常 toggle 时已经自动生效,这里手动触发)"
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            from ...addon import get_manager
            mgr = get_manager()
            obj = context.object
            if mgr is None or obj is None:
                self.report({'ERROR'}, "manager or active object not ready")
                return {'CANCELLED'}
            module = mgr.get_module(obj.name)
            if module is None:
                self.report({'ERROR'}, f"no module {obj.name!r}")
                return {'CANCELLED'}
            cfg = obj.conveyor
            if hasattr(module, "set_speed"):
                module.set_speed(cfg.target_speed)
            if hasattr(module, "set_direction"):
                d = 1 if cfg.direction == "forward" else -1
                module.set_direction(d)
            if hasattr(module, "set_friction"):
                module.set_friction(cfg.friction)
            if hasattr(module, "set_running"):
                module.set_running(cfg.running)
            self.report({'INFO'}, "conveyor cfg applied")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class CONVEYOR_PT_panel(bpy.types.Panel):
    bl_label = "Conveyor"
    bl_idname = "OBJECT_PT_conveyor"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from . import naming
        obj = context.object
        return obj is not None and naming.is_host(obj)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        obj = context.object
        if obj is None:
            return
        cfg = obj.conveyor
        if cfg is None:
            layout.label(text="ConveyorProperty unavailable.", icon="ERROR")
            return

        # ---- Enabled + Refresh ----
        layout.label(text="Conveyor host configuration", icon="OBJECT_DATAMODE")
        row = layout.row(align=True)
        if _has_prop(cfg, "enabled"):
            row.prop(cfg, "enabled", text="Enabled")
        row.operator("conveyor.refresh", text="Refresh", icon="FILE_REFRESH")

        if not getattr(cfg, "enabled", True):
            layout.label(text="Enable the addon to configure host references.",
                         icon="INFO")
            return

        layout.separator()

        # ---- Host refs ----
        box = layout.box()
        box.label(text="Host references", icon="OUTLINER_OB_EMPTY")
        for field, label in (
            ("drive_roller", "drive_roller"),
            ("idler_roller", "idler_roller"),
            ("belt", "belt"),
        ):
            if _has_prop(cfg, field):
                box.prop(cfg, field)
            else:
                box.label(text=f"{label}: <not available>")

        # ---- Motion parameters ----
        motion_box = layout.box()
        motion_box.label(text="Motion", icon="DRIVER")
        if _has_prop(cfg, "target_speed"):
            motion_box.prop(cfg, "target_speed", text="target_speed (mm/s)")
        if _has_prop(cfg, "direction"):
            motion_box.prop(cfg, "direction", text="direction")
        if _has_prop(cfg, "friction"):
            motion_box.prop(cfg, "friction", text="friction (0..1)")

        # ---- Control ----
        ctrl_box = layout.box()
        ctrl_box.label(text="Control", icon="HANDLETYPE_AUTO_CLAMP_VEC")
        row = ctrl_box.row(align=True)
        if _has_prop(cfg, "running"):
            row.prop(cfg, "running", text="Running", toggle=True)
        ctrl_box.operator("conveyor.apply", icon="FILE_TICK")

        # ---- Runtime state ----
        state_box = layout.box()
        state_box.label(text="Runtime state", icon="PLAY")
        try:
            from ...addon import get_manager
            mgr = get_manager()
            module = mgr.get_module(obj.name) if mgr is not None else None
        except Exception:
            module = None
        if module is None or not hasattr(module, "snapshot"):
            state_box.label(text="(not registered yet — click Refresh)")
        else:
            snap = module.snapshot()
            state_box.label(text=f"state: {snap.get('state', '?')}")
            state_box.label(
                text="running: {}".format(
                    "1" if snap.get("running") else "0"
                )
            )
            d = snap.get("direction", 0)
            state_box.label(
                text="direction: {} ({})".format(
                    d, "forward" if d == 1 else "reverse",
                )
            )
            state_box.label(
                text=f"target_speed: {snap.get('target_speed', 0):.3f} mm/s"
            )
            state_box.label(
                text="friction: {:.3f}".format(snap.get("friction", 0.0))
            )
            # uv_offset 已从 state_push snapshot 移除(带宽考虑),
            # 面板直读实例内部值,显示不变。
            state_box.label(
                text="uv_offset: {:.4f}".format(
                    float(getattr(module, "_uv_offset", 0.0))
                )
            )
            driven_names = snap.get("driven_names") or []
            state_box.label(
                text="driven: {} ({})".format(
                    len(driven_names), ", ".join(driven_names) or "-"
                )
            )
            state_box.label(text=f"stop_reason: {snap.get('stop_reason', '')}")


# ---- register/unregister ----


def register() -> None:
    bpy.utils.register_class(CONVEYOR_OT_refresh)
    bpy.utils.register_class(CONVEYOR_OT_apply)
    bpy.utils.register_class(CONVEYOR_PT_panel)


def unregister() -> None:
    for cls in (
        CONVEYOR_PT_panel,
        CONVEYOR_OT_apply,
        CONVEYOR_OT_refresh,
    ):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


__all__ = ["register", "unregister"]