# -*- coding: utf-8 -*-
"""VacuumNozzle 命名约定。

与 ``LinearAxis`` / ``Cylinder`` 一致:host Empty 命名按 ``VacuumNozzle*``
前缀识别(后面数字作为 ``module_id`` 后缀;无数字 → 默认 "1")。
"""

from __future__ import annotations


_PREFIX = "VacuumNozzle"


def is_host(obj) -> bool:
    """Return True iff ``obj`` is a VacuumNozzle host.

    Host 同时是三种角色:**配置容器**(host_obj 上的 ``vacuum_nozzle``
    PropertyGroup,只有一个 ``enabled`` 开关)、**感应区锚点**(虚拟立方体
    的坐标系就是 host 的局部坐标系)、以及被吸物体的**新父级**。
    不参与任何运动计算 —— 所以 type 无关,EMPTY / MESH /
    CAMERA 都可以。命名约定:名字以 ``VacuumNozzle`` 开头。

    注意:LinearAxis / RotateAxis / Cylinder 都用 EMPTY host(其设计
    强制 EMPTY,见各自 ``naming.py``);VacuumNozzle 允许更灵活 ——
    艺术家通常把代表吸嘴的**圆柱体 MESH** 直接命名 ``VacuumNozzle1``,
    它自己就是感应区锚点(旧版的 ``sensor_mesh`` 指针已删除)。

    ``is_host`` 也是 **overlay 的识别依据**(不靠 ``sensor_type``),
    所以即使吸嘴还没开真空(未注册 runtime),overlay 照样画得出来。
    """
    if obj is None:
        return False
    try:
        name = getattr(obj, "name", "") or ""
    except ReferenceError:
        # 已删除的 datablock(stale 引用)—— 当作不是 host,而不是抛异常。
        # overlay / 扫描循环里会遍历任意对象,这里必须容错。
        return False
    except Exception:
        return False
    return name.startswith(_PREFIX)


def module_id_from_name(name: str) -> str:
    """从 host name 推 module_id: ``VacuumNozzle`` 之后的部分(无 → "1")。

    与 :mod:`modules.LinearAxix.naming` / :mod:`modules.Cylinder.naming`
    同构;但本函数目前仅在 tests / offline build_module 中用,生产路径
    discovery 走 host_obj.name 直传(``module_id=host.name``)。
    """
    if not isinstance(name, str) or not name.startswith(_PREFIX):
        return "1"
    suffix = name[len(_PREFIX):]
    return suffix if suffix else "1"


__all__ = ["is_host", "module_id_from_name"]