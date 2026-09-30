"""RotateAxis module package — rotational-motion runtime implementation."""

from __future__ import annotations

try:
    from .rotate_axis import RotateAxisRuntime
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.RotateAxis.rotate_axis import RotateAxisRuntime  # noqa: F401

__all__ = ["RotateAxisRuntime"]