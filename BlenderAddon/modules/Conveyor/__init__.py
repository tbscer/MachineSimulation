# -*- coding: utf-8 -*-
"""Conveyor module package —— 综合对象 conveyor 运行时实现。

公共 API:
- :class:`ConveyorModule` —— 运行时模块
- :func:`register` / :func:`unregister` —— PropertyGroup + UI 注册入口
"""

from __future__ import annotations

try:
    from .runtime import ConveyorModule
except ImportError:  # pragma: no cover
    from modules.Conveyor.runtime import ConveyorModule  # noqa: F401


def register() -> None:
    """注册 ConveyorProperty 到 ``bpy.types.Object.conveyor`` + UI panel。

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


__all__ = ["ConveyorModule", "register", "unregister"]