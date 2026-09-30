"""LinearAxis Blender addon — entry point (V0_3).

V0_3 architecture
-----------------
- :class:`LinearAxisProperty` (``modules/LinearAxix/component.py``) —
  ``bpy.types.PropertyGroup`` attached to every ``bpy.types.Object``
  via ``Object.linear_axis``. Holds typed configuration:
  ``PointerProperty(Object)`` refs for slider / rail / sensors / shim,
  ``FloatProperty`` for speed / stroke.
- :class:`SimulationManager` (``simulation_manager.py``) — owns the
  ``bpy.app.timers`` tick loop and registers every :class:`LinearAxis`.
- :class:`LinearAxis` (``modules/LinearAxix/axis.py``) — **passive**
  axis runtime. Owns slider / rail / sensors / shim components, exposes
  ``update(dt)`` and command methods (``move_to`` / ``set_velocity`` /
  ``home`` / ``stop``).
- ``discovery.py`` — unified discovery entry, runs **once** at addon
  enable. Walks ``scene.objects`` and delegates each host to its
  module's discoverer; the LinearAxis discoverer
  (``modules/LinearAxix/discovery.py``) auto-fills empty
  ``PointerProperty`` fields from naming convention, builds a
  ``LinearAxis`` per ``LinearAxis*`` EMPTY host and registers it with
  the manager.

The previous V0_1 addon scanned every tick and used heavy custom
properties (``linear_axis_role`` / ``linear_axis_id`` etc.). That
configuration burden is gone in V0_3.

Discovery is one-shot: if you edit the scene, call
``axis_ops.refresh_axes()`` to rebuild, or disable+re-enable the
addon. There is no per-tick scene scan.

This addon is independent of ``Twin.Mvp/``. No network, no protocol,
no C# references.
"""

from __future__ import annotations

from .framework import BaseSimulationModule, SimulationCommand

bl_info = {
    "name": "LinearAxis",
    "version": (0, 3, 0),
    "author": "MotionSimulate",
    "blender": (5, 1, 0),
    "category": "Development",
    "description": (
        "Generic linear-axis digital-twin unit. V0_3: SimulationManager "
        "+ LinearAxisProperty PropertyGroup + passive LinearAxis. "
        "Attach a LinearAxis to any EMPTY named LinearAxis*; the addon "
        "auto-discovers Slider / Rail / Sensor_* children by naming."
    ),
}


def register() -> None:
    """Blender addon hook: start timers and initialise the simulation."""
    from . import addon as _a
    _a.register()


def unregister() -> None:
    """Blender addon hook: stop timers, clear manager, drop PropertyGroup."""
    from . import addon as _a
    _a.unregister()