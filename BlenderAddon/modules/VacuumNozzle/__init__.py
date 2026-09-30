# -*- coding: utf-8 -*-
"""VacuumNozzle module package —— 真空吸嘴运行时实现。

公共 API:
- :class:`VacuumNozzleModule` —— 运行时模块
- :func:`register` / :func:`unregister` —— PropertyGroup + UI 注册入口
"""

from __future__ import annotations

try:
    from .runtime import VacuumNozzleModule
except ImportError:  # pragma: no cover
    from modules.VacuumNozzle.runtime import VacuumNozzleModule  # noqa: F401


def register() -> None:
    """注册 VacuumNozzleProperty 到 ``bpy.types.Object.vacuum_nozzle`` + UI panel。

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


__all__ = ["VacuumNozzleModule", "register", "unregister"]