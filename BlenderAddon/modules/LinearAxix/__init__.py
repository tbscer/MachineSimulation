"""LinearAxis module package — linear-motion runtime implementation.

Contents:

- ``axis.py``     — :class:`LinearAxis` passive runtime
- ``axis_ops.py`` — command/state helpers (send_command / read_state ...)
- ``component.py``— ``LinearAxisProperty`` PropertyGroup configuration
- ``discovery.py``— host auto-fill + runtime construction (discoverer)
- ``naming.py``   — LinearAxis naming conventions
- ``rail.py``     — ``RailComponent`` (travel envelope, LinearAxis-specific)
- ``slider.py``   — ``SliderComponent`` (moving body, LinearAxis-specific)
- ``ui.py``       — Blender panel / operators (requires bpy; loaded by addon.py)
"""

from __future__ import annotations

try:
    from .axis import LinearAxis, VALID_ACTIONS
    from . import naming
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.LinearAxix.axis import LinearAxis, VALID_ACTIONS  # noqa: F401
    from modules.LinearAxix import naming  # noqa: F401

__all__ = ["LinearAxis", "VALID_ACTIONS", "naming"]