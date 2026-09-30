# -*- coding: utf-8 -*-
"""Cylinder module package —— 综合对象 cylinder 运行时实现。

公共 API:
- :class:`CylinderModule` —— 运行时模块
- :func:`register` / :func:`unregister` —— PropertyGroup + UI 注册入口
"""

from __future__ import annotations

try:
    from .cylinder import CylinderModule
except ImportError:  # pragma: no cover
    from modules.Cylinder.cylinder import CylinderModule  # noqa: F401


def register() -> None:
    """注册 CylinderProperty 到 ``bpy.types.Object.cylinder`` + UI panel。

    在 ``MotionSimulation.addon.register`` 里调用。
    """
    from . import component, ui
    component.register()
    ui.register()


def unregister() -> None:
    """反注册(幂等)。"""
    from . import component, ui
    ui.unregister()
    component.unregister()


__all__ = ["CylinderModule", "register", "unregister"]