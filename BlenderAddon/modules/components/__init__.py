"""Shared pluggable components for all motion modules.

Per the design spec (组件可插拔), these low-level components are
framework-level building blocks reusable by any module runtime —
not owned by a single axis:

- :class:`BaseSensor`           — abstract sensor base class
- :class:`UTypeSensor`          — U-slot style overlap detector
- :class:`TriggerShimComponent` — clean AABB source for sensors
- :class:`ApproachSensor`       — virtual 1×1×3 cube trigger detector
- :class:`CollisionEngine`      — physics-engine-based collision detection
- :mod:`geometry`               — pure geometry helpers
- :mod:`sensor.geometry`        — sensor-specific geometric helpers
                                   (DetectionVolume, AABB, …)

LinearAxis-specific motion components (``SliderComponent`` /
``RailComponent``) live at :mod:`modules.LinearAxix`.
"""

from __future__ import annotations

try:
    from .sensor import (
        BaseSensor,
        UTypeSensor,
        UTypeGeometry,
        UTypeGeometryAnalyzer,
        AABB,
        DetectionVolume,
    )
    from .trigger_shim import TriggerShimComponent
    from .collision import (
        CollisionEngine,
        SCENE_COLLISION_KEY,
        STOP_REASON_COLLISION,
        is_obstacle_candidate,
        bvh_overlap_exists,
    )
    from .approach_sensor import (
        ApproachSensor,
        register_rna as register_approach_sensor_rna,
        unregister_rna as unregister_approach_sensor_rna,
        register_overlay as register_approach_sensor_overlay,
        unregister_overlay as unregister_approach_sensor_overlay,
    )
    from .vacuum_nozzle import (
        VacuumNozzleSensor,
        register_rna as register_vacuum_nozzle_rna,
        unregister_rna as unregister_vacuum_nozzle_rna,
        register_overlay as register_vacuum_nozzle_overlay,
        unregister_overlay as unregister_vacuum_nozzle_overlay,
    )
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.components.sensor import (  # noqa: F401
        BaseSensor,
        UTypeSensor,
        UTypeGeometry,
        UTypeGeometryAnalyzer,
        AABB,
        DetectionVolume,
    )
    from modules.components.trigger_shim import TriggerShimComponent  # noqa: F401
    from modules.components.collision import (  # noqa: F401
        CollisionEngine,
        SCENE_COLLISION_KEY,
        STOP_REASON_COLLISION,
        is_obstacle_candidate,
        bvh_overlap_exists,
    )
    from modules.components.approach_sensor import (  # noqa: F401
        ApproachSensor,
        register_rna as register_approach_sensor_rna,
        unregister_rna as unregister_approach_sensor_rna,
        register_overlay as register_approach_sensor_overlay,
        unregister_overlay as unregister_approach_sensor_overlay,
    )
    from modules.components.vacuum_nozzle import (  # noqa: F401
        VacuumNozzleSensor,
        register_rna as register_vacuum_nozzle_rna,
        unregister_rna as unregister_vacuum_nozzle_rna,
        register_overlay as register_vacuum_nozzle_overlay,
        unregister_overlay as unregister_vacuum_nozzle_overlay,
    )

__all__ = [
    "BaseSensor",
    "UTypeSensor",
    "UTypeGeometry",
    "UTypeGeometryAnalyzer",
    "AABB",
    "DetectionVolume",
    "TriggerShimComponent",
    "CollisionEngine",
    "SCENE_COLLISION_KEY",
    "STOP_REASON_COLLISION",
    "is_obstacle_candidate",
    "bvh_overlap_exists",
    "ApproachSensor",
    "register_approach_sensor_rna",
    "unregister_approach_sensor_rna",
    "register_approach_sensor_overlay",
    "unregister_approach_sensor_overlay",
    "VacuumNozzleSensor",
    "register_vacuum_nozzle_rna",
    "unregister_vacuum_nozzle_rna",
    "register_vacuum_nozzle_overlay",
    "unregister_vacuum_nozzle_overlay",
]