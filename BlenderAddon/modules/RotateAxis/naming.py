# -*- coding: utf-8 -*-
"""RotateAxis host 命名判定 —— 仅保留 ``is_host`` 一项。

历史
----
本模块曾经是“整套命名约定”的单一来源：``RotateCenter`` /
``RotateShaft`` / ``Rotator`` / ``Sensor_Home`` 之类的 alias tuple
``auto_fill`` 据此从 host 的 children 里**猜**出哪个是 rotator / center
/ sensor，然后写回 cfg pointer。

那个设计有两个问题：

1. **靠名猜**：美术必须在场景里把 mesh 命名为 ``RotateCenter``、
   ``Rotator``、``Sensor_Home.R`` 这种“魔术名字”才能让插件工作；一旦
   改名 / 复制 / 用不同前缀，discoverer 直接跳过、面板 Home 按钮报
   “找不到轴 ID”。过去几次 bug（``RotateAxis.001`` 被自动重命名、
   ``Shaft.R`` 短名等）都源于此。
2. **优先级错乱**：cfg pointer 才是 ground truth（用户在 panel 里手动
   关联的 ``PointerProperty``），命名约定**应该**是最后才用的兜底，但
   在原代码里它们却平级、互相 fallback，导致 panel poll 用 startswith
   但 discoverer 用严格 regex 时会冒出“面板可见但 Home 报 KeyError”。

本模块砍到只保留 :func:`is_host`：host Empty 仍然需要按
``RotateAxis`` 前缀 + 非空 id 识别（axis id 解析的语义离不开名字），
其他一切（center / rotator / sensor 命名约定）全部删除。
Runtime 拿到 host 之后，**全部依赖 cfg pointer**：如果用户在 panel 里没
配，discoverer 直接跳过；命名匹配这种隐式查找不再存在。

新增 / 改名 / 移动 mesh 时，美术只需在 Object Properties → ``Rotate
Axis`` 面板里调整 pointer，无需考虑命名约定。
"""

from __future__ import annotations


HOST_PREFIX = "RotateAxis"


def is_host(obj) -> bool:
    """Return True iff ``obj`` qualifies as a RotateAxis host.

    A host is an EMPTY-typed Blender object whose name starts with
    :data:`HOST_PREFIX` and has a non-empty axis id (suffix after
    the prefix, with a single leading ``_`` or ``.`` stripped).
    ``parse_host_id`` 的逻辑被内联在这里 —— 它不需要被生产代码
    单独调用，所以一并删除避免死代码。

    接受的名字形如::

        "RotateAxis1"        -> True   (axis id = "1")
        "RotateAxis_2"       -> True   (axis id = "2")
        "RotateAxis.001"     -> True   (axis id = ".001")
        "RotateAxis.R"       -> True   (axis id = ".R")
        "RotateAxis-Turret"  -> True   (axis id = "-Turret")
        "RotateAxis"         -> False  (no axis id)
        "Slider"             -> False  (wrong prefix)
    """
    if obj is None:
        return False
    # Avoid importing bpy: duck-type for tests.
    obj_type = getattr(obj, "type", None)
    if obj_type != "EMPTY":
        return False
    name = getattr(obj, "name", "")
    if not isinstance(name, str) or not name.startswith(HOST_PREFIX):
        return False
    suffix = name[len(HOST_PREFIX):]
    # 原 ``parse_host_id`` 只 strip 下划线，保留点号：``RotateAxis.R`` ->
    # ``".R"``、``RotateAxis.001`` -> ``".001"`` 都是合法 axis id。
    stripped = suffix.lstrip("_")
    return bool(stripped)


__all__ = ["HOST_PREFIX", "is_host"]