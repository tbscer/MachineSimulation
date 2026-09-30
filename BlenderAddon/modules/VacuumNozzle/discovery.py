# -*- coding: utf-8 -*-
"""VacuumNozzle discovery:host 校验 + runtime 构造。

参照 :mod:`modules.Cylinder.discovery` 的契约:

- :data:`FLAG` —— 已发现标志位(``_vacuum_nozzle_discovered``)
- :func:`is_host` —— 由 :mod:`modules.VacuumNozzle.naming` 提供
- :func:`discover` —— 构造 :class:`VacuumNozzleModule`,失败返回 ``None``
- :func:`clear_flag` —— 清标志

校验要点
--------
1. ``host`` 上必须挂着 ``vacuum_nozzle`` PropertyGroup。
   **不看 ``enabled``** —— 原因见下节。
2. **没有任何必填 pointer** —— 感应区锚定在 host 自己身上,可吸对象由
   运行时的自动扫描决定,所以配置面只剩一个 bool。
3. ``host`` 不能是它自己的祖先(病态层级),构造时由 runtime 的扫描循环
   兜底,这里不额外校验。

为什么 discovery **不**按 ``enabled`` 门控
------------------------------------------
``enabled``(= Vacuum On/Off 开关)是**运行时状态**,不是"这个 host 要
不要建 runtime"的开关。两者混在一起会有一个静默陷阱:

- ``VacuumNozzleProperty.enabled`` 的默认值是 **False**(Off);
- discovery 是**一次性**的(插件 enable / 用户点 Refresh),没有 per-tick 扫描;
- 于是最普通的操作顺序 —— 先 enable 插件,再在面板里点 "Vacuum On" —— 里,
  discovery 早就跑完并因为 ``enabled == False`` 跳过了这个吸嘴,
  模块永远不注册,点 On 之后**什么都不发生**(不吸附、不报警)。

所以这里只认"host 身份 + PropertyGroup 存在",On/Off 完全交给
:meth:`VacuumNozzleModule.update` 每 tick 镜像 cfg 后的运行时决策 ——
点 On 后下一个 tick 就扫描吸附(≤1/30 s),不需要任何 Refresh。
RPC ``set_vacuum_enabled`` 同理:插件一启动模块就在册,调用即可用。

注:其它 module kind(LinearAxis / RotateAxis / Cylinder)的 ``enabled``
默认是 **True**,所以它们的同一处门控不会咬人;VacuumNozzle 的默认是
False,门控就等于"默认永不注册"。

与旧版的差别
------------
旧版要求 ``sensor_mesh`` + ``trigger_obj`` 两个必填 pointer,并在
``sensor_mesh`` 上盖 ``sensor_type = "vacuum_nozzle"`` 给 overlay 认。
新版把三者全部去掉:

- ``sensor_mesh`` → 感应区锚点改为 host 自己;
- ``trigger_obj`` → 删除(它会静默绕过自动扫描,旧 ``.blend`` 里普遍残留);
- ``sensor_type`` 标记 → overlay 改按名字前缀(:func:`naming.is_host`)识别。

因此环境里遗留的 ``sensor_type = "vacuum_nozzle"`` / ``trigger_obj``
数据会被**完全忽略**(对吸嘴本身无影响)。
"""

from __future__ import annotations

from typing import Optional

try:
    from .runtime import VacuumNozzleModule
    from .naming import is_host
except ImportError:  # pragma: no cover
    # 离线/直接脚本 import 路径
    from modules.VacuumNozzle.runtime import VacuumNozzleModule  # noqa: F401
    from modules.VacuumNozzle.naming import is_host  # noqa: F401


FLAG = "_vacuum_nozzle_discovered"


def auto_fill(host) -> bool:
    """没有必填 pointer —— 恒返回 True(保留函数是为了 discovery 契约一致)。

    历史上这里校验 ``sensor_mesh`` / ``trigger_obj`` 是否齐全;新版两者
    都已删除,配置面只剩 ``enabled``,所以 auto_fill 只检查 PropertyGroup
    是否存在。
    """
    return getattr(host, "vacuum_nozzle", None) is not None


def build(host, collision_engine=None, scene=None, module_source=None):
    """构造 :class:`VacuumNozzleModule`,失败返回 None。"""
    cfg = getattr(host, "vacuum_nozzle", None)
    if cfg is None:
        return None
    try:
        return VacuumNozzleModule(
            host_obj=host,
            collision_engine=collision_engine,
            module_id=host.name,
            scene=scene,
            module_source=module_source,
        )
    except Exception as exc:
        print(f"[VacuumNozzle] {host.name}: build failed: {exc!r}")
        return None


def discover(host, collision_engine=None) -> Optional[VacuumNozzleModule]:
    """VacuumNozzle host 的发现入口。

    **不按 ``cfg.enabled`` 门控** —— 只要 host 上挂着 ``vacuum_nozzle``
    PropertyGroup 就构建 runtime。理由见模块 docstring 的
    「为什么 discovery **不**按 ``enabled`` 门控」:
    ``enabled`` 默认 False + discovery 一次性 ⇒ 否则"先开插件再点
    Vacuum On"这条最普通的路径永远注册不上,表现为"点 On 不吸附"。

    Off 的吸嘴寄存器里的 runtime 处于 ``STATE_DISABLED``:每 tick 只做
    ``_release_all()`` + overlay 标志写回,不扫描候选,开销可忽略。
    """
    cfg = getattr(host, "vacuum_nozzle", None)
    if cfg is None:
        return None
    return build(host, collision_engine=collision_engine)


def clear_flag(host) -> int:
    if host.get(FLAG, None) is None:
        return 0
    try:
        del host[FLAG]
    except Exception:
        try:
            host[FLAG] = None
        except Exception:
            pass
    return 1


__all__ = [
    "FLAG",
    "is_host",
    "auto_fill",
    "build",
    "discover",
    "clear_flag",
]