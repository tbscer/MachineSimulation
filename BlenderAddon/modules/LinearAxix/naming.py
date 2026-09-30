# -*- coding: utf-8 -*-
"""LinearAxis host 命名判定 —— 仅保留 ``is_host`` 一项。

历史与 :mod:`modules.RotateAxis.naming` 完全对齐：本模块曾经维护
``Slider`` / ``Rail`` / ``Sensor_Home`` 等 alias tuple，``auto_fill``
据此从 host children 里猜出哪些是 slider / rail / sensor，再写回 cfg
pointer。但命名约定隐式查找带来的 bug（Duplicate 后 ``LinearAxis.001``
被严格 regex 拒绝、面板 poll 用 startswith 接收、Home 报"找不到轴 ID"）
已经反复出现过几次。

彻底移除命名匹配：runtime 拿到 host 之后完全依赖 cfg pointer（用户在
panel 里手动关联的 ``PointerProperty``）；host 本身仍需按 ``LinearAxis``
前缀 + 非空 id 识别（axis id 解析的语义离不开名字），其余 alias 全部删除。
"""

from __future__ import annotations


HOST_PREFIX = "LinearAxis"


def is_host(obj) -> bool:
    """Return True iff ``obj`` qualifies as a LinearAxis host.

    A host is an EMPTY-typed Blender object whose name starts with
    :data:`HOST_PREFIX` and has a non-empty axis id (suffix after the
    prefix, with a single leading ``_`` or ``.`` stripped).

    接受的名字形如::

        "LinearAxis1"        -> True
        "LinearAxis_2"       -> True
        "LinearAxis.001"     -> True
        "LinearAxisX"        -> True
        "LinearAxis"         -> False
        "RotateAxis1"        -> False  (wrong prefix)
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