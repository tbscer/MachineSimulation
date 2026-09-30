"""Module factory package.

Each sub-package hosts one runtime module implementation:

- ``LinearAxix/``  — :class:`LinearAxis` linear-motion runtime
- ``RotateAxis/``  — :class:`RotateAxisRuntime` rotational-motion runtime
- ``components/``  — shared pluggable components

:func:`build_module` is the unified factory entry point used by the
top-level ``discovery.build_module`` to create a runtime for a
discovered host object. Real scene discovery goes through
``discovery.discover_and_register`` which delegates each host to its
module's discoverer (which auto-fills PropertyGroup fields and
constructs the runtime). The factory here is a **fallback** for
callers that bypass discovery — typically offline unit tests.

The fallback recognises LinearAxis / RotateAxis host *names* and
returns a passive runtime with no components wired (the caller is
responsible for filling in the host's ``linear_axis`` PropertyGroup
and re-running discovery to obtain a usable runtime).
"""

from __future__ import annotations


try:
    from .LinearAxix.axis import LinearAxis
    from .RotateAxis.rotate_axis import RotateAxisRuntime
    from .Cylinder.cylinder import CylinderModule
    from .VacuumNozzle.runtime import VacuumNozzleModule
    from .ApproachSensor.runtime import ApproachSensorModule
    from .Conveyor.runtime import ConveyorModule
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.LinearAxix.axis import LinearAxis  # noqa: F401
    from modules.RotateAxis.rotate_axis import RotateAxisRuntime  # noqa: F401
    from modules.Cylinder.cylinder import CylinderModule  # noqa: F401
    from modules.VacuumNozzle.runtime import VacuumNozzleModule  # noqa: F401
    from modules.ApproachSensor.runtime import ApproachSensorModule  # noqa: F401
    from modules.Conveyor.runtime import ConveyorModule  # noqa: F401


def build_module(host_obj):
    """Create a passive module runtime for a Blender host object (fallback).

    Used by ``tests/test_framework.py`` and any caller that wants a
    module reference without running the full auto-fill / discovery
    pipeline. The returned runtime has no center / slider / sensors
    wired; callers must use the discoverer-based path for production
    rigs.
    """
    name = getattr(host_obj, "name", "")
    host_type = getattr(host_obj, "type", "")

    if host_type == "EMPTY" and name.startswith("RotateAxis"):
        axis_id = name.split("RotateAxis")[-1] or "1"
        # Construct with empty components; the discoverer will replace
        # this with a fully wired runtime if the host is registered.
        # ``slider=None`` is accepted as a backward-compat alias for
        # ``rotator=None``; ``RotateAxisRuntime.__init__`` sets
        # ``self.slider = self.rotator`` internally.
        try:
            return RotateAxisRuntime(
                host_obj,
                center=None,  # type: ignore[arg-type]
                rotator=None,  # type: ignore[arg-type]
                slider=None,  # type: ignore[arg-type]
                sensors=[],
                axis_id=axis_id,
            )
        except TypeError:
            # Older signature (post-rename but pre-alias) — drop the
            # ``slider`` kwarg.
            return RotateAxisRuntime(
                host_obj,
                center=None,  # type: ignore[arg-type]
                rotator=None,  # type: ignore[arg-type]
                sensors=[],
                axis_id=axis_id,
            )

    if host_type == "EMPTY" and name.startswith("LinearAxis"):
        axis_id = name.split("LinearAxis")[-1] or "1"
        return LinearAxis(
            host_obj, slider=None, rail=None, sensors=[], axis_id=axis_id
        )

    if host_type == "EMPTY" and name.startswith("Cylinder"):
        # Fallback for offline tests; real discovery goes through
        # ``modules.Cylinder.discovery``. Returns an unconfigured
        # instance. Constructing without real refs would fail (axis_dir
        # derivation needs two approach sensors), so we return None —
        # the discoverer is the supported path.
        return None

    if host_type == "EMPTY" and name.startswith("VacuumNozzle"):
        # Fallback for offline tests; real discovery goes through
        # ``modules.VacuumNozzle.discovery``. 新版没有必填 pointer
        # (感应区锚定在 host 自己身上),所以这里直接构造一个未接线
        # 的实例 —— 它在 Off 态下什么都不做,On 后需要 ``scene`` /
        # ``module_source`` 才能真正扫描吸附。
        vacuum_id = name.split("VacuumNozzle")[-1] or "1"
        try:
            return VacuumNozzleModule(
                host_obj=host_obj,
                module_id=vacuum_id,
            )
        except Exception:
            return None

    if name.startswith("ApproachSensor"):
        # Fallback for offline tests; real discovery goes through
        # ``modules.ApproachSensor.discovery``. ApproachSensor module 不要求
        # ``trigger_obj`` pointer (每 tick 自动扫描场景),也不限制
        # host type (MESH / EMPTY 都接受);离线测试直接构造一个未
        # 接 collision engine 的实例即可。
        sensor_id = name.split("ApproachSensor")[-1] or "1"
        try:
            return ApproachSensorModule(
                host_obj=host_obj,
                module_id=sensor_id,
            )
        except Exception:
            return None

    if host_type == "EMPTY" and name.startswith("Conveyor"):
        # Fallback for offline tests; real discovery goes through
        # ``modules.Conveyor.discovery``. 离线测试直接构造一个未接
        # collision engine 的实例即可(drive_roller / idler_roller / belt
        # 留 None,update() 走 stale 哨兵会立即 set _alive=False)。
        conveyor_id = name.split("Conveyor")[-1] or "1"
        try:
            return ConveyorModule(
                host_obj=host_obj,
                drive_roller=None,
                idler_roller=None,
                belt=None,
                module_id=conveyor_id,
            )
        except Exception:
            return None

    return None