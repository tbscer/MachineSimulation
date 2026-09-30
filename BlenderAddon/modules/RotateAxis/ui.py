"""Blender UI helpers for the RotateAxis addon.

Defines the Object Properties panel that exposes the host object's
RotateAxis configuration fields, runtime state, and convenient
controls. Mirrors the LinearAxis UI so both panels feel like one
family — the host's ``module_kind`` PropertyGroup enum is what
selects which panel (or which subset) renders.
"""

from __future__ import annotations

import json

import bpy


class ROTATEAXIS_OT_refresh_axes(bpy.types.Operator):
    bl_idname = "rotate_axis.refresh_axes"
    bl_label = "Refresh RotateAxis"
    bl_description = "Re-run scene discovery and refresh RotateAxis host references"

    def execute(self, context: bpy.types.Context) -> set[str]:
        from .axis_ops import refresh_axes

        try:
            discovered = refresh_axes()
            self.report({'INFO'}, f"Discovered {discovered} rotate axis/axes")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class ROTATEAXIS_OT_home(bpy.types.Operator):
    bl_idname = "rotate_axis.home"
    bl_label = "Home"
    bl_description = "Send a homing command to this RotateAxis host"

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}
        cfg = obj.rotate_axis
        from .axis_ops import send_command, AxisBlockedError

        axis_id = obj.name

        try:
            send_command(
                axis_id, "home",
                home_direction=getattr(cfg, "home_direction", -1),
                velocity=getattr(cfg, "angular_home_speed", 10.0),
            )
            self.report({'INFO'}, "Homing command sent")
        except AxisBlockedError:
            self.report(
                {'ERROR'},
                "RotateAxis is BLOCKED by collision — press 'Clear collision' "
                "in the MotionSimulation panel first",
            )
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class ROTATEAXIS_OT_stop(bpy.types.Operator):
    bl_idname = "rotate_axis.stop"
    bl_label = "Stop"
    bl_description = "Stop motion on this RotateAxis host"

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}

        from .axis_ops import send_command, AxisBlockedError

        axis_id = obj.name

        try:
            send_command(axis_id, "stop")
            self.report({'INFO'}, "Stop command sent")
        except AxisBlockedError:
            self.report(
                {'ERROR'},
                "RotateAxis is BLOCKED by collision — press 'Clear collision' "
                "in the MotionSimulation panel first",
            )
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class ROTATEAXIS_OT_move_to(bpy.types.Operator):
    bl_idname = "rotate_axis.move_to"
    bl_label = "Move To"
    bl_description = "Command this RotateAxis host to rotate to a target angle"

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}

        cfg = obj.rotate_axis
        from .axis_ops import send_command, AxisBlockedError

        axis_id = obj.name

        try:
            send_command(
                axis_id,
                "move_to",
                target_angle=getattr(cfg, "move_target_angle", 0.0),
                velocity=getattr(cfg, "angular_speed", 0.5),
            )
            self.report({'INFO'}, f"Move-to {getattr(cfg, 'move_target_angle', 0.0)} deg sent")
        except AxisBlockedError:
            self.report(
                {'ERROR'},
                "RotateAxis is BLOCKED by collision — press 'Clear collision' "
                "in the MotionSimulation panel first",
            )
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class ROTATEAXIS_OT_detect_axis(bpy.types.Operator):
    bl_idname = "rotate_axis.detect_axis"
    bl_label = "Detect Rotation Axis"
    bl_description = (
        "Auto-detect the rotation axis from the RotateCenter mesh's "
        "bounding box (longest local dimension) and update the axis index."
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}
        cfg = obj.rotate_axis
        from . import discovery as rotate_discovery

        previous = int(getattr(cfg, "rotate_axis_index", 2))
        detected = rotate_discovery.detect_rotate_axis(cfg, obj)
        label = rotate_discovery._AXIS_LABELS[detected]
        self.report(
            {'INFO'},
            f"Detected rotation axis = {detected} (local {label})"
            + ("" if detected == previous else f" (was {previous})"),
        )
        return {'FINISHED'}


class ROTATEAXIS_PT_object_panel(bpy.types.Panel):
    bl_label = "Rotate Axis"
    bl_idname = "OBJECT_PT_rotate_axis"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # 面板和发现器对 "什么是合法 host" 的判定必须保持一致 —— 否则
        # 用户看到面板但 Home/Stop 等按钮点下去会走到
        # ``axis_ops._require_axis`` 报 ``KeyError("No RotateAxis with
        # axis_id=…")``。统一用 ``naming.is_host``，它内部基于
        # ``parse_host_id``，接受 ``RotateAxis1`` / ``RotateAxis_2`` /
        # ``RotateAxis.001`` / ``RotateAxis-Turret`` 等等所有以
        # ``RotateAxis`` 为前缀且后缀非空的名字；旧的
        # ``startswith("RotateAxis")`` 会把不能被发现的副本也显示出来。
        from . import naming as _rotate_naming
        obj = context.object
        return obj is not None and _rotate_naming.is_host(obj)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        obj = context.object
        if obj is None:
            return

        cfg = obj.rotate_axis
        layout.label(text="RotateAxis host configuration", icon="OBJECT_DATAMODE")
        row = layout.row(align=True)

        def _has_prop(pg, name: str) -> bool:
            try:
                return name in type(pg).bl_rna.properties
            except Exception:
                return hasattr(pg, name)

        if _has_prop(cfg, "enabled"):
            row.prop(cfg, "enabled", text="Enabled")
        else:
            row.label(text="enabled: <not available>")
        row.operator("rotate_axis.refresh_axes", text="Refresh", icon="FILE_REFRESH")

        has_enabled = _has_prop(cfg, "enabled")
        enabled_val = getattr(cfg, "enabled", True) if has_enabled else True
        if not enabled_val:
            layout.label(
                text="Enable the addon to configure host references.",
                icon="INFO",
            )
            return

        layout.separator()

        box = layout.box()
        box.label(text="Host references", icon="OUTLINER_OB_EMPTY")
        if _has_prop(cfg, "rotate_center"):
            box.prop(cfg, "rotate_center", text="Rotate Center (static pivot)")
        else:
            box.label(text="rotate_center: <not available>")
        if _has_prop(cfg, "rotator"):
            box.prop(cfg, "rotator", text="Rotator")
        else:
            box.label(text="rotator: <not available>")
        # No parent relationship is required between Rotator and
        # RotateCenter — the runtime rotates the Rotator around the
        # centre's world pivot directly, so the rig's scene graph is
        # entirely up to the artist.
        if _has_prop(cfg, "trigger_shim"):
            box.prop(cfg, "trigger_shim")
        else:
            box.label(text="trigger_shim: <not available>")

        sensor_box = layout.box()
        sensor_box.label(text="Sensors", icon="MESH_ICOSPHERE")
        if _has_prop(cfg, "rotate_home_sensor"):
            sensor_box.prop(cfg, "rotate_home_sensor")
        else:
            sensor_box.label(text="rotate_home_sensor: <not available>")
        if _has_prop(cfg, "rotate_pos_limit_sensor"):
            sensor_box.prop(cfg, "rotate_pos_limit_sensor")
        else:
            sensor_box.label(text="rotate_pos_limit_sensor: <not available>")
        if _has_prop(cfg, "rotate_neg_limit_sensor"):
            sensor_box.prop(cfg, "rotate_neg_limit_sensor")
        else:
            sensor_box.label(text="rotate_neg_limit_sensor: <not available>")

        # ---- 每个 sensor 的“感应方向” ----
        from . import discovery as _rotate_discovery
        direction_box = sensor_box.box()
        direction_box.label(text="Detection direction", icon="EMPTY_AXIS")
        for _kind, _field in (
            ("home", "rotate_home_sensor"),
            ("pos",  "rotate_pos_limit_sensor"),
            ("neg",  "rotate_neg_limit_sensor"),
        ):
            _ptr = _rotate_discovery._resolve_or_lookup(cfg, obj, _field)
            if _ptr is None:
                continue
            _row = direction_box.row(align=True)
            _row.label(text=f"{_ptr.name}:")
            try:
                _row.prop(_ptr, "sensor_direction", text="axis")
            except Exception:
                _row.label(text="(sensor_direction unavailable)")
            # 触发厚度（米）：见 LinearAxis 面板同款说明。
            _row.prop(_ptr, "sensor_thickness", text="thick(m)")

        motion_box = layout.box()
        motion_box.label(text="Motion parameters", icon="DRIVER")
        if _has_prop(cfg, "rotate_stroke"):
            motion_box.prop(cfg, "rotate_stroke")
        else:
            motion_box.label(text="rotate_stroke: <not available>")
        if _has_prop(cfg, "angular_speed"):
            motion_box.prop(cfg, "angular_speed")
        else:
            motion_box.label(text="angular_speed: <not available>")
        if _has_prop(cfg, "angular_home_speed"):
            motion_box.prop(cfg, "angular_home_speed")
        else:
            motion_box.label(text="angular_home_speed: <not available>")
        if _has_prop(cfg, "rotate_axis_index"):
            motion_box.prop(cfg, "rotate_axis_index")
            motion_box.label(
                text="0 = local X, 1 = local Y, 2 = local Z",
                icon='INFO',
            )
            motion_box.operator("rotate_axis.detect_axis", icon="ORPHAN_DATA")
        else:
            motion_box.label(text="rotate_axis_index: <not available>")

        layout.separator()

        state_box = layout.box()
        state_box.label(text="Runtime state", icon="PLAY")
        from . import discovery as rotate_discovery

        rotator_obj = rotate_discovery._resolve_or_lookup(cfg, obj, rotate_discovery.ROTATOR_FIELD)
        if rotator_obj is None:
            state_box.label(text="No rotator object assigned or not resolvable yet.")
        else:
            axis_state = rotator_obj.get("axis_state", "<unknown>")
            axis_current_angle = rotator_obj.get("axis_current_angle", "?")
            axis_velocity = rotator_obj.get("axis_velocity", "?")
            stop_reason = rotator_obj.get("axis_stop_reason", "")
            has_target = rotator_obj.get("axis_has_target", False)
            target_angle = rotator_obj.get("axis_target_angle", 0.0)
            sensor_json = rotator_obj.get("axis_sensors", "{}")
            try:
                sensor_states = json.loads(sensor_json)
            except ValueError:
                sensor_states = {}

            state_box.label(text=f"State: {axis_state}")
            state_box.label(text=f"Angle: {axis_current_angle}")
            state_box.label(text=f"Angular velocity: {axis_velocity}")
            state_box.label(text=f"Stop reason: {stop_reason}")
            if has_target:
                state_box.label(text=f"Target angle: {target_angle}")
            if sensor_states:
                sb = state_box.box()
                sb.label(text="Sensor triggers", icon="RADIOBUT_ON")
                for name, triggered in sensor_states.items():
                    sb.label(text=f"{name}: {'ON' if triggered else 'OFF'}")

        layout.separator()
        control_box = layout.box()
        control_box.label(text="Commands", icon="ARMATURE_DATA")
        if _has_prop(cfg, "home_direction"):
            control_box.prop(cfg, "home_direction", text="Home dir")
        else:
            control_box.label(text="home_direction: <not available>")
        row = control_box.row(align=True)
        row.operator("rotate_axis.home", icon="FILE_TICK")
        row.operator("rotate_axis.stop", icon="CANCEL")

        move_box = control_box.box()
        move_box.label(text="Move-to", icon="DRIVER")
        if _has_prop(cfg, "move_target_angle"):
            move_box.prop(cfg, "move_target_angle")
        else:
            move_box.label(text="move_target_angle: <not available>")
        move_box.operator("rotate_axis.move_to", icon="SNAP_INCREMENT")

        layout.separator()
        layout.label(
            text="Leave empty to auto-fill by naming convention.",
            icon="INFO",
        )


def register() -> None:
    bpy.utils.register_class(ROTATEAXIS_OT_refresh_axes)
    bpy.utils.register_class(ROTATEAXIS_OT_home)
    bpy.utils.register_class(ROTATEAXIS_OT_stop)
    bpy.utils.register_class(ROTATEAXIS_OT_move_to)
    bpy.utils.register_class(ROTATEAXIS_OT_detect_axis)
    bpy.utils.register_class(ROTATEAXIS_PT_object_panel)


def unregister() -> None:
    for cls in (
        ROTATEAXIS_PT_object_panel,
        ROTATEAXIS_OT_detect_axis,
        ROTATEAXIS_OT_move_to,
        ROTATEAXIS_OT_stop,
        ROTATEAXIS_OT_home,
        ROTATEAXIS_OT_refresh_axes,
    ):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass