# -*- coding: utf-8 -*-
"""VacuumNozzle host 的 Object Properties 面板。

在 PROPERTIES > object context 下,当前选中物体是 ``VacuumNozzle*`` host
时显示(判定与 discovery 共用 :func:`modules.VacuumNozzle.naming.is_host`,
避免"面板看得到但 discovery 不认"的不一致)。

字段分组
--------
- **Vacuum control** —— ``enabled``(= On/Off)开关 + ``Refresh``
- **Sensing area** —— 直接编辑 host 自己的
  ``vacuum_nozzle_normal_axis`` / ``vacuum_nozzle_cube_size`` /
  ``vacuum_nozzle_working_face_center``。这三个 RNA 属性就是感应区
  虚拟立方体的定义(与 ApproachSensor 同构),3D 视口里的 overlay
  实时画出同一几何。
- **Runtime state** —— 从 :class:`VacuumNozzleModule.snapshot()` 读
  state / on / sensing / holding,并提供 ``Force Release``

为什么需要这个面板
------------------
没有这个面板时,host 上的 PropertyGroup 虽然在
(``bpy.types.Object.vacuum_nozzle``),却没有任何可编辑入口 —— 表现就是
"Object 面板里找不到 VacuumNozzle"。感应区几何同理:RNA 注册在
``bpy.types.Object`` 上,但只有本面板给出可见的编辑入口。
"""

from __future__ import annotations

import bpy


def _has_prop(pg, name: str) -> bool:
    """PropertyGroup 字段存在性检查(容错 deferred RNA)。"""
    try:
        return name in type(pg).bl_rna.properties
    except Exception:
        return hasattr(pg, name)


class VACUUMNOZZLE_OT_refresh(bpy.types.Operator):
    """重跑场景 discovery;让 ``enabled`` / 指针改动立刻生效。"""

    bl_idname = "vacuum_nozzle.refresh"
    bl_label = "Refresh VacuumNozzle"
    bl_description = (
        "Re-run scene discovery and (re)build VacuumNozzle modules — "
        "新建 / 重命名 / 重新挂载吸嘴后需要，与 Vacuum On 无关"
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


class VACUUMNOZZLE_OT_force_release(bpy.types.Operator):
    """立即强制释放吸嘴当前持有的物体(无需等下一个 tick 的条件检查)。"""

    bl_idname = "vacuum_nozzle.force_release"
    bl_label = "Force Release"
    bl_description = (
        "立即释放所有吸住的物体,放回场景目录(BLOCKED 状态下也可执行,"
        "是 collision 锁定期间的应急出口)"
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
            if module is None or not hasattr(module, "force_release"):
                self.report(
                    {'ERROR'},
                    f"no VacuumNozzle module for {obj.name!r} — 点面板里的 "
                    f"Refresh 重新发现场景后再试",
                )
                return {'CANCELLED'}
            module.force_release()
            self.report({'INFO'}, "released")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class VACUUMNOZZLE_PT_panel(bpy.types.Panel):
    bl_label = "Vacuum Nozzle"
    bl_idname = "OBJECT_PT_vacuum_nozzle"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        from . import naming
        obj = context.object
        return obj is not None and naming.is_host(obj)

    def _draw_sensing_area(self, layout, obj) -> None:
        """感应区几何(锚点 = host 自己)。"""
        box = layout.box()
        box.label(text="Sensing area (anchored on this object)",
                  icon="MESH_ICOSPHERE")
        row = box.row(align=True)
        try:
            row.prop(obj, "vacuum_nozzle_normal_axis", text="axis")
        except Exception:
            row.label(text="(normal_axis unavailable)")
        try:
            row.prop(obj, "vacuum_nozzle_cube_size", text="cube")
        except Exception:
            row.label(text="(cube_size unavailable)")
        try:
            box.prop(obj, "vacuum_nozzle_working_face_center",
                     text="face_center")
        except Exception:
            pass
        box.label(
            text="Uses this object's local axes — same model as ApproachSensor.",
            icon="INFO",
        )

    def _draw_mount(self, layout, obj) -> None:
        """显示吸嘴被谁带着运动(只读,便于确认吸附后跟随谁)。"""
        try:
            parent = getattr(obj, "parent", None)
        except Exception:
            parent = None
        box = layout.box()
        box.label(text="Mounted on (read-only)", icon="CON_TRANSLIKE")
        box.label(text=f"{parent.name if parent is not None else '<scene root>'}")

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        obj = context.object
        if obj is None:
            return
        cfg = getattr(obj, "vacuum_nozzle", None)
        if cfg is None:
            layout.label(text="VacuumNozzleProperty unavailable.", icon="ERROR")
            return

        # ---- On/Off + Refresh ----
        layout.label(text="Vacuum control", icon="OBJECT_DATAMODE")
        row = layout.row(align=True)
        if _has_prop(cfg, "enabled"):
            row.prop(cfg, "enabled", text="Vacuum On", toggle=True)
        else:
            row.label(text="enabled: <not available>")
        row.operator("vacuum_nozzle.refresh", text="Refresh",
                     icon="FILE_REFRESH")

        # ---- 感应区几何 ----
        layout.separator()
        self._draw_sensing_area(layout, obj)

        # ---- 挂载关系 ----
        layout.separator()
        self._draw_mount(layout, obj)

        # ---- Runtime state ----
        layout.separator()
        state_box = layout.box()
        state_box.label(text="Runtime state", icon="PLAY")
        try:
            from ...addon import get_manager
            mgr = get_manager()
            module = mgr.get_module(obj.name) if mgr is not None else None
        except Exception:
            module = None
        if module is None or not hasattr(module, "snapshot"):
            state_box.label(
                text="(not registered — 点 Refresh 重新发现场景)",
                icon="INFO",
            )
            return
        try:
            snap = module.snapshot()
        except Exception as exc:
            state_box.label(text=f"snapshot failed: {exc}", icon="ERROR")
            return
        state_box.label(text=f"state: {snap.get('state', '?')}")
        state_box.label(text="on: {}".format("1" if snap.get("enabled") else "0"))
        state_box.label(text="sensing: {}".format("1" if snap.get("sensing") else "0"))
        held_count = snap.get("held_count", 0)
        state_box.label(
            text="holding: {} ({})".format(
                "1" if snap.get("holding") else "0", held_count
            )
        )
        held_names = snap.get("held_names") or []
        if held_names:
            state_box.label(text="picked: " + ", ".join(str(n) for n in held_names))
        stop_reason = snap.get("stop_reason", "")
        if stop_reason:
            state_box.label(text=f"stop_reason: {stop_reason}")
        state_box.operator("vacuum_nozzle.force_release", icon="X")


# ---- register/unregister ----


def register() -> None:
    bpy.utils.register_class(VACUUMNOZZLE_OT_refresh)
    bpy.utils.register_class(VACUUMNOZZLE_OT_force_release)
    bpy.utils.register_class(VACUUMNOZZLE_PT_panel)


def unregister() -> None:
    for cls in (
        VACUUMNOZZLE_PT_panel,
        VACUUMNOZZLE_OT_force_release,
        VACUUMNOZZLE_OT_refresh,
    ):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


__all__ = ["register", "unregister"]