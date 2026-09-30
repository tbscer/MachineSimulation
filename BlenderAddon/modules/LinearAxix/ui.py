"""Blender UI helpers for the LinearAxis addon.

Defines the Object Properties panel that exposes the host object's
LinearAxis configuration fields, runtime state, and convenient controls.
"""

from __future__ import annotations

import json

import bpy


class LINEARAXIS_OT_refresh_axes(bpy.types.Operator):
    bl_idname = "linear_axis.refresh_axes"
    bl_label = "Refresh LinearAxis"
    bl_description = "Re-run scene discovery and refresh LinearAxis host references"

    def execute(self, context: bpy.types.Context) -> set[str]:
        from .axis_ops import refresh_axes

        try:
            discovered = refresh_axes()
            self.report({'INFO'}, f"Discovered {discovered} axis/axes")
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class LINEARAXIS_OT_home(bpy.types.Operator):
    bl_idname = "linear_axis.home"
    bl_label = "Home"
    bl_description = "Send a homing command to this LinearAxis host"

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}
        cfg = obj.linear_axis
        from .axis_ops import send_command, AxisBlockedError

        # Use the full host name as the axis id — the manager now
        # registers every axis under its full host name (e.g.
        # ``LinearAxis1``) so external systems can refer to it
        # directly without re-parsing.
        axis_id = obj.name

        try:
            send_command(axis_id, "home", home_direction=getattr(cfg, "home_direction", -1),
                         velocity=getattr(cfg, "home_speed", 0.1))
            self.report({'INFO'}, "Homing command sent")
        except AxisBlockedError:
            self.report(
                {'ERROR'},
                "LinearAxis is BLOCKED by collision — press "
                "'Clear collision' in the MotionSimulation panel "
                "first",
            )
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class LINEARAXIS_OT_stop(bpy.types.Operator):
    bl_idname = "linear_axis.stop"
    bl_label = "Stop"
    bl_description = "Stop motion on this LinearAxis host"

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
                "LinearAxis is BLOCKED by collision — press "
                "'Clear collision' in the MotionSimulation panel "
                "first",
            )
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class LINEARAXIS_OT_move_to(bpy.types.Operator):
    bl_idname = "linear_axis.move_to"
    bl_label = "Move To"
    bl_description = "Command this LinearAxis host to move to a target position at the configured speed"

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.object
        if obj is None:
            self.report({'ERROR'}, "No active object")
            return {'CANCELLED'}

        cfg = obj.linear_axis
        from . import discovery as linear_discovery
        from .axis_ops import send_command, AxisBlockedError

        axis_id = obj.name
        target_x = float(getattr(cfg, "move_target_x", 0.0))
        speed = float(getattr(cfg, "speed", 0.5))
        # Resolve the slider to read its current X so we can compute
        # ``duration = distance / speed``. If the slider is not yet
        # wired, fall back to a 1 s duration to keep the operator
        # always callable.
        slider_obj = linear_discovery._resolve_or_lookup(cfg, obj, "slider")
        if slider_obj is not None:
            try:
                current_x = float(slider_obj.location.x)
            except Exception:
                current_x = 0.0
            distance = abs(target_x - current_x)
        else:
            distance = 0.0
        duration_s = max(distance / speed, 1e-6) if speed > 0.0 else 1.0

        try:
            send_command(
                axis_id,
                "move_to",
                target_x=target_x,
                velocity=speed,
            )
            # 场景约定为 mm（1 Blender 单位 = 1 mm）：target_x / speed
            # 的数值即 mm / mm·s⁻¹，这里只在文案里显式标注单位。
            self.report({'INFO'},
                        f"Move-to {target_x} mm at speed {speed} mm/s "
                        f"(duration {duration_s:.3f}s) sent")
        except AxisBlockedError:
            self.report(
                {'ERROR'},
                "LinearAxis is BLOCKED by collision — press "
                "'Clear collision' in the MotionSimulation panel "
                "first",
            )
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
        return {'FINISHED'}


class LINEARAXIS_PT_object_panel(bpy.types.Panel):
    bl_label = "Linear Axis"
    bl_idname = "OBJECT_PT_linear_axis"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        # 面板和发现器对 "什么是合法 host" 的判定必须保持一致 —— 否则
        # 用户看到面板但 Home/Stop 等按钮点下去会走到
        # ``axis_ops._require_axis`` 报 ``KeyError("No LinearAxis with
        # axis_id=…")``。统一用 ``naming.is_host``，它内部基于
        # ``parse_host_id``，接受 ``LinearAxis1`` / ``LinearAxis_2`` /
        # ``LinearAxis.001`` / ``LinearAxis-ConveyorA`` 等等所有以
        # ``LinearAxis`` 为前缀且后缀非空的名字；旧的
        # ``startswith("LinearAxis")`` 会把不能被发现的副本也显示出来。
        from . import naming as _linear_naming
        obj = context.object
        return obj is not None and _linear_naming.is_host(obj)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        obj = context.object
        if obj is None:
            return

        cfg = obj.linear_axis
        layout.label(text="LinearAxis host configuration", icon="OBJECT_DATAMODE")
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
        row.operator("linear_axis.refresh_axes", text="Refresh", icon="FILE_REFRESH")

        # Avoid directly reading cfg.enabled unless it's available in RNA.
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
        def _has_prop(pg, name: str) -> bool:
            try:
                return name in type(pg).bl_rna.properties
            except Exception:
                return hasattr(pg, name)

        if _has_prop(cfg, "slider"):
            box.prop(cfg, "slider")
        else:
            box.label(text="slider: <not available>")
        if _has_prop(cfg, "rail"):
            box.prop(cfg, "rail")
        else:
            box.label(text="rail: <not available>")
        if _has_prop(cfg, "trigger_shim"):
            box.prop(cfg, "trigger_shim")
        else:
            box.label(text="trigger_shim: <not available>")

        sensor_box = layout.box()
        sensor_box.label(text="Sensors", icon="MESH_ICOSPHERE")
        if _has_prop(cfg, "home_sensor"):
            sensor_box.prop(cfg, "home_sensor")
        else:
            sensor_box.label(text="home_sensor: <not available>")
        if _has_prop(cfg, "pos_sensor"):
            sensor_box.prop(cfg, "pos_sensor")
        else:
            sensor_box.label(text="pos_sensor: <not available>")
        if _has_prop(cfg, "neg_sensor"):
            sensor_box.prop(cfg, "neg_sensor")
        else:
            sensor_box.label(text="neg_sensor: <not available>")

        # ---- 每个 sensor 的“感应方向” ----
        from . import discovery as _linear_discovery
        direction_box = sensor_box.box()
        direction_box.label(text="Detection direction", icon="EMPTY_AXIS")
        for _kind, _field in (
            ("home", "home_sensor"),
            ("pos",  "pos_sensor"),
            ("neg",  "neg_sensor"),
        ):
            _ptr = _linear_discovery._resolve_or_lookup(cfg, obj, _field)
            if _ptr is None:
                continue
            _row = direction_box.row(align=True)
            _row.label(text=f"{_ptr.name}:")
            try:
                _row.prop(_ptr, "sensor_direction", text="axis")
            except Exception:
                _row.label(text="(sensor_direction unavailable)")
            # 触发厚度：沿 direction 的薄片厚度（Blender 单位）。在 mm 场景
            # （1 BU = 1 mm）里填 1.0 即 1 mm 厚；overlay 的琥珀色薄片线框由
            # direction + thickness 决定触发体形状。
            _row.prop(_ptr, "sensor_thickness", text="thick (BU)")

        motion_box = layout.box()
        # 运动参数均按 mm / mm·s⁻¹ 理解（场景约定 1 Blender 单位 = 1 mm）：
        # speed = 运行速度（mm/s）、home_speed = 回零速度（mm/s）。
        motion_box.label(text="Motion parameters (mm / mm/s)", icon="DRIVER")
        if _has_prop(cfg, "speed"):
            motion_box.prop(cfg, "speed", text="speed (mm/s)")
        else:
            motion_box.label(text="speed: <not available>")
        if _has_prop(cfg, "home_speed"):
            motion_box.prop(cfg, "home_speed", text="home_speed (mm/s)")
        else:
            motion_box.label(text="home_speed: <not available>")

        layout.separator()

        state_box = layout.box()
        state_box.label(text="Runtime state", icon="PLAY")
        # Resolve the slider PointerProperty safely; in some contexts
        # reading a PointerProperty returns a _PropertyDeferred wrapper
        # which does not expose dict-like access. Use this module's
        # discovery helpers to get a real Object reference when possible.
        from . import discovery as linear_discovery

        slider_obj = linear_discovery._resolve_or_lookup(cfg, obj, "slider")
        if slider_obj is None:
            state_box.label(text="No slider object assigned or not resolvable yet.")
        else:
            # slider_obj is a real bpy.types.Object; read custom props safely.
            axis_state = slider_obj.get("axis_state", "<unknown>")
            axis_current_x = slider_obj.get("axis_current_x", "?")
            axis_velocity = slider_obj.get("axis_velocity", "?")
            stop_reason = slider_obj.get("axis_stop_reason", "")
            has_target = slider_obj.get("axis_has_target", False)
            target_x = slider_obj.get("axis_target_x", 0.0)
            sensor_json = slider_obj.get("axis_sensors", "{}")
            try:
                sensor_states = json.loads(sensor_json)
            except ValueError:
                sensor_states = {}

            # 状态数值由 _write_state_props 镜像；场景 1 BU = 1 mm，故
            # axis_current_x / axis_target_x 数值即 mm，velocity 即 mm/s。
            state_box.label(text=f"State: {axis_state}")
            state_box.label(text=f"Position: {axis_current_x} mm")
            state_box.label(text=f"Velocity: {axis_velocity} mm/s")
            state_box.label(text=f"Stop reason: {stop_reason}")
            if has_target:
                state_box.label(text=f"Target: {target_x} mm")
            if sensor_states:
                sensor_box = state_box.box()
                sensor_box.label(text="Sensor triggers", icon="RADIOBUT_ON")
                for name, triggered in sensor_states.items():
                    sensor_box.label(text=f"{name}: {'ON' if triggered else 'OFF'}")

        layout.separator()
        control_box = layout.box()
        control_box.label(text="Commands", icon="ARMATURE_DATA")
        if _has_prop(cfg, "home_direction"):
            control_box.prop(cfg, "home_direction", text="Home dir")
        else:
            control_box.label(text="home_direction: <not available>")
        row = control_box.row(align=True)
        row.operator("linear_axis.home", icon="FILE_TICK")
        row.operator("linear_axis.stop", icon="CANCEL")

        move_box = control_box.box()
        move_box.label(text="Move-to (mm)", icon="DRIVER")
        if _has_prop(cfg, "move_target_x"):
            move_box.prop(cfg, "move_target_x", text="target (mm)")
        else:
            move_box.label(text="move_target_x: <not available>")
        move_box.operator("linear_axis.move_to", icon="SNAP_INCREMENT")

        layout.separator()
        layout.label(
            text="Leave empty to auto-fill by naming convention.",
            icon="INFO",
        )


def register() -> None:
    bpy.utils.register_class(LINEARAXIS_OT_refresh_axes)
    bpy.utils.register_class(LINEARAXIS_OT_home)
    bpy.utils.register_class(LINEARAXIS_OT_stop)
    bpy.utils.register_class(LINEARAXIS_OT_move_to)
    bpy.utils.register_class(LINEARAXIS_PT_object_panel)
    # 所有 sensor 物体上的感应方向（X / Y / Z）。在 LinearAxis host 的
    # panel 中为每个已配置的 sensor 暴露出来，同时 sensor 实例会在
    # 运行时主动读取该值。
    bpy.types.Object.sensor_direction = bpy.props.EnumProperty(
        name="Sensor Direction",
        description=(
            "Detection axis of the U-type sensor. The sensor looks along "
            "this axis; the AABB-vs-shim overlap test is applied using a "
            "thin slice of the sensor's local BB centered at its centroid."
        ),
        items=[
            ("X", "X (along world X)", "Sensor looks along its local X axis"),
            ("Y", "Y (along world Y)", "Sensor looks along its local Y axis"),
            ("Z", "Z (along world Z)", "Sensor looks along its local Z axis"),
        ],
        default="X",
    )
    # 触发体沿感应方向的厚度（Blender 单位）。m 制工作流默认 0.001 即
    # 1 mm（shim 长跨薄平面时有保证的逐帧重叠，不会漏检）；mm 工作流
    # （1 BU = 1 mm）请填 1.0 表示 1 mm。触发区 = sensor 沿 direction 方向
    # 的中间位置处的薄块，厚度即此值，横向 = sensor 在垂直法向两轴上的
    # 完整尺寸。
    if not hasattr(bpy.types.Object, "sensor_thickness"):
        bpy.types.Object.sensor_thickness = bpy.props.FloatProperty(
            name="Sensor Thickness",
            description=(
                "触发体沿感应方向的厚度（Blender 单位；m 制场景默认 0.001 "
                "即 1 mm；mm 场景 1 BU = 1 mm 请填 1.0 表示 1 mm）。触发区 = "
                "sensor 沿 direction 方向的中间位置处的薄块，厚度即此值，横向"
                "不超过 sensor 尺寸。"
            ),
            subtype="DISTANCE",
            default=0.001,
            min=1e-6,
            precision=4,
        )


def unregister() -> None:
    try:
        del bpy.types.Object.sensor_direction
    except Exception:
        pass
    try:
        del bpy.types.Object.sensor_thickness
    except Exception:
        pass
    for cls in (
        LINEARAXIS_PT_object_panel,
        LINEARAXIS_OT_move_to,
        LINEARAXIS_OT_stop,
        LINEARAXIS_OT_home,
        LINEARAXIS_OT_refresh_axes,
    ):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass