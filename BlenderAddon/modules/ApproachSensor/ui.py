"""ApproachSensor host 的 Object Properties 面板。

在 PROPERTIES > object context 下, 选中物体是 ``ApproachSensor*`` host 时显示
(判定与 discovery 共用 :func:`modules.ApproachSensor.naming.is_host`,
避免"面板看得到但 discovery 不认"的不一致)。

字段分组
--------
- **Enabled** —— 一次性 enabled 开关 (与 LinearAxis / Cylinder 同构)
- **Trigger group name** —— 可选扩展位, 本轮不参与 runtime 行为
- **Geometry** —— Approach sensor 的感应盒配置 (working_face_center 中心
  偏移 + cube_size 长宽高)。感应盒是**中心对齐的轴对齐盒** (三维独立),
  描述 sensor 周围的"感应区"; artist 在面板上设长宽高 + 偏离中心位置即可
  (详见 :mod:`modules.ApproachSensor.runtime`)。
- **Runtime state** —— state / is_triggered / triggered_obj_name
- **Operator buttons** —— Refresh (重跑 discovery)
"""

from __future__ import annotations

import bpy


def _has_prop(pg, name: str) -> bool:
    """PropertyGroup 字段存在性检查 (容错 deferred RNA)。"""
    try:
        return name in type(pg).bl_rna.properties
    except Exception:
        return hasattr(pg, name)


class APPROACH_SENSOR_OT_refresh(bpy.types.Operator):
    """Re-run scene discovery; 让 approach sensor host 的 pointer 改动立刻生效。"""

    bl_idname = "approach_sensor.refresh"
    bl_label = "Refresh Approach Sensor"
    bl_description = (
        "Re-run scene discovery and rebuild ApproachSensor host references"
    )

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


class APPROACH_SENSOR_PT_panel(bpy.types.Panel):
    bl_label = "Approach Sensor"
    bl_idname = "OBJECT_PT_approach_sensor"
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
        cfg = obj.approach_sensor
        if cfg is None:
            layout.label(text="ApproachSensorProperty unavailable.", icon="ERROR")
            return

        # ---- Enabled + Refresh ----
        layout.label(
            text="Approach sensor host configuration", icon="OBJECT_DATAMODE"
        )
        row = layout.row(align=True)
        if _has_prop(cfg, "enabled"):
            row.prop(cfg, "enabled", text="Enabled")
        row.operator(
            "approach_sensor.refresh", text="Refresh", icon="FILE_REFRESH"
        )

        if not getattr(cfg, "enabled", True):
            layout.label(
                text="Enable the addon to register this approach sensor module.",
                icon="INFO",
            )
            return

        layout.separator()

        # ---- Cfg fields ----
        cfg_box = layout.box()
        cfg_box.label(text="Module configuration", icon="SETTINGS")
        if _has_prop(cfg, "trigger_group_name"):
            cfg_box.prop(cfg, "trigger_group_name", text="trigger group")

        # ---- Geometry ----
        geom_box = layout.box()
        geom_box.label(
            text="Approach sensor geometry", icon="MESH_ICOSPHERE"
        )
        geom_box.label(
            text="(中心对齐的轴对齐盒: cube_size (长宽高) + face_center (中心偏移))",
            icon="INFO",
        )
        try:
            geom_box.prop(obj, "approach_sensor_cube_size", text="cube size (l, w, h)")
        except Exception:
            geom_box.label(text="(cube_size unavailable)")
        try:
            geom_box.prop(
                obj, "approach_sensor_working_face_center",
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
            try:
                snap = module.snapshot()
            except Exception:
                snap = {}
            if not isinstance(snap, dict):
                snap = {}
            state_box.label(text=f"state: {snap.get('state', '?')}")
            triggered = snap.get("is_triggered", False)
            state_box.label(
                text="is_triggered: {}".format(
                    "1" if triggered else "0"
                ),
                icon=("RADIOBUT_ON" if triggered else "RADIOBUT_OFF"),
            )
            triggered_name = snap.get("triggered_obj_name", "") or "(none)"
            state_box.label(text=f"triggered_obj: {triggered_name}")


# ---- register/unregister ----


def register() -> None:
    bpy.utils.register_class(APPROACH_SENSOR_OT_refresh)
    bpy.utils.register_class(APPROACH_SENSOR_PT_panel)


def unregister() -> None:
    for cls in (APPROACH_SENSOR_PT_panel, APPROACH_SENSOR_OT_refresh):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


__all__ = ["register", "unregister"]