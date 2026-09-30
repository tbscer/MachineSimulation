# -*- coding: utf-8 -*-
"""Cylinder host 的 Object Properties 面板。

在 PROPERTIES > object context 下,选中物体是 ``Cylinder*`` host 时显示。

字段分组:
- **Host refs** —— work_bar / touch_shim / approach_sensor_1 / approach_sensor_2
- **Motion parameters** —— target_speed (mm/s)
- **Output signals** —— output_1 / output_2 toggle(写入即生效)
- **Approach sensors** —— 两个 approach sensor 的几何配置
  (cube_size 长宽高 / working_face_center 中心偏移 / trigger_obj)
- **Runtime state** —— current_state (位置: approach_sensor_1 /
  approach_sensor_2 / unknown) + state (runtime) + velocity + stop_reason
"""

from __future__ import annotations

import bpy


def _has_prop(pg, name: str) -> bool:
    """PropertyGroup 字段存在性检查(容错 deferred RNA)。"""
    try:
        return name in type(pg).bl_rna.properties
    except Exception:
        return hasattr(pg, name)


class CYLINDER_OT_refresh(bpy.types.Operator):
    """Re-run scene discovery;让 cylinder host 的 pointer 改动立刻生效。"""

    bl_idname = "cylinder.refresh"
    bl_label = "Refresh Cylinder"
    bl_description = "Re-run scene discovery and rebuild Cylinder host references"

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
        return {'FINISHED'}


class CYLINDER_OT_set_outputs(bpy.types.Operator):
    """便捷按钮:把当前 panel 的 output_1/output_2 推到 instance。"""

    bl_idname = "cylinder.set_outputs"
    bl_label = "Apply Outputs"
    bl_description = (
        "把当前面板上的 output_1/output_2 写回 CylinderProperty "
        "(通常 toggle 时已经自动生效,这里手动触发)"
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
            cfg = obj.cylinder
            module.set_outputs(cfg.output_1, cfg.output_2)
            self.report({'INFO'}, "outputs applied")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class CYLINDER_PT_panel(bpy.types.Panel):
    bl_label = "Cylinder"
    bl_idname = "OBJECT_PT_cylinder"
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
        cfg = obj.cylinder
        if cfg is None:
            layout.label(text="CylinderProperty unavailable.", icon="ERROR")
            return

        # ---- Enabled + Refresh ----
        layout.label(text="Cylinder host configuration", icon="OBJECT_DATAMODE")
        row = layout.row(align=True)
        if _has_prop(cfg, "enabled"):
            row.prop(cfg, "enabled", text="Enabled")
        row.operator("cylinder.refresh", text="Refresh", icon="FILE_REFRESH")

        if not getattr(cfg, "enabled", True):
            layout.label(text="Enable the addon to configure host references.",
                         icon="INFO")
            return

        layout.separator()

        # ---- Host refs ----
        box = layout.box()
        box.label(text="Host references", icon="OUTLINER_OB_EMPTY")
        for field, label in (
            ("work_bar", "work_bar"),
            ("touch_shim", "touch_shim"),
            ("approach_sensor_1", "approach_sensor_1"),
            ("approach_sensor_2", "approach_sensor_2"),
        ):
            if _has_prop(cfg, field):
                box.prop(cfg, field)
            else:
                box.label(text=f"{label}: <not available>")

        # ---- Motion parameters ----
        motion_box = layout.box()
        motion_box.label(text="Motion parameters (mm/s)", icon="DRIVER")
        if _has_prop(cfg, "target_speed"):
            motion_box.prop(cfg, "target_speed", text="speed (mm/s)")

        # ---- Output signals ----
        out_box = layout.box()
        out_box.label(text="Output signals", icon="HANDLETYPE_AUTO_CLAMP_VEC")
        row = out_box.row(align=True)
        if _has_prop(cfg, "output_1"):
            row.prop(cfg, "output_1", text="output_1")
        if _has_prop(cfg, "output_2"):
            row.prop(cfg, "output_2", text="output_2")
        out_box.operator("cylinder.set_outputs", icon="FILE_TICK")

        # ---- Approach sensors ----
        sensors_box = layout.box()
        sensors_box.label(text="Approach sensors", icon="MESH_ICOSPHERE")
        for field in ("approach_sensor_1", "approach_sensor_2"):
            sensor_obj = getattr(cfg, field, None)
            if sensor_obj is None:
                continue
            sub = sensors_box.box()
            sub.label(text=sensor_obj.name, icon="OUTLINER_OB_MESH")
            try:
                row = sub.row(align=True)
                row.prop(
                    sensor_obj, "approach_sensor_cube_size", text="cube (l,w,h)",
                )
            except Exception:
                sub.label(text="(cube_size unavailable)")
            try:
                sub.prop(
                    sensor_obj, "approach_sensor_working_face_center",
                    text="center offset",
                )
            except Exception:
                pass

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
            state_box.label(text=f"state: {snap['state']}")
            state_box.label(text=f"current_state: {snap['current_state']}")
            #state_box.label(text=f"velocity (BU/s): {snap['velocity']:.3f}")
            state_box.label(text=f"stop_reason: {snap['stop_reason']}")


# ---- register/unregister ----


def register() -> None:
    bpy.utils.register_class(CYLINDER_OT_refresh)
    bpy.utils.register_class(CYLINDER_OT_set_outputs)
    bpy.utils.register_class(CYLINDER_PT_panel)


def unregister() -> None:
    for cls in (
        CYLINDER_PT_panel,
        CYLINDER_OT_set_outputs,
        CYLINDER_OT_refresh,
    ):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


__all__ = ["register", "unregister"]