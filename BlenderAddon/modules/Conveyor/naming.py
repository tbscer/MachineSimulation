# -*- coding: utf-8 -*-
"""Conveyor host 命名判定 —— 与 LinearAxis / Cylinder 对齐。

仅保留 :func:`is_host` 一项:host 必须是 EMPTY、名字以 ``Conveyor`` 开头
且有非空后缀(如 ``Conveyor1`` / ``Conveyor_Main`` / ``Conveyor.001``)。
"""

from __future__ import annotations


HOST_PREFIX = "Conveyor"


def is_host(obj) -> bool:
    """Return True iff ``obj`` is a Conveyor host.

    接受的名字形如::

        "Conveyor1"        -> True
        "Conveyor_Main"    -> True
        "Conveyor.001"     -> True
        "Conveyor"         -> False  (空后缀)
        "LinearAxis1"      -> False  (前缀错)
    """
    if obj is None:
        return False
    obj_type = getattr(obj, "type", None)
    if obj_type != "EMPTY":
        return False
    name = getattr(obj, "name", "")
    if not isinstance(name, str) or not name.startswith(HOST_PREFIX):
        return False
    suffix = name[len(HOST_PREFIX):]
    stripped = suffix.lstrip("_")
    return bool(stripped)


__all__ = ["HOST_PREFIX", "is_host"]