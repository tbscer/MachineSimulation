# -*- coding: utf-8 -*-
"""Conveyor discovery:cfg pointer 校验 + runtime 构造。

参照 :mod:`modules.Cylinder.discovery` 的契约:

- :data:`FLAG`  ——  已发现标志位 (``_conveyor_discovered``)
- :func:`is_host` —— 由 :mod:`modules.Conveyor.naming` 提供
- :func:`discover` —— 构造 :class:`ConveyorModule`,失败返回 ``None``
- :func:`clear_flag` —— 清标志

校验要点
--------
1. ``host.conveyor.enabled`` 必须为 True(默认 True,同 LinearAxis 风格)。
2. 必填 pointer:``drive_roller`` / ``idler_roller`` / ``belt``。
   任一缺失则在控制台打印提示,跳过。
3. 两个 roller 不能是同一个 Object(否则 belt_dir 无定义)。
4. :class:`ConveyorModule` 构造时还会校验两 roller 世界位置不重合
   (否则推导 belt_dir 会 raise,同 Cylinder approach_sensor 的失败模式)。
"""

from __future__ import annotations

from typing import Optional

try:
    from ... import discovery as _framework_discovery
    from .runtime import ConveyorModule
    from .naming import is_host
except ImportError:  # pragma: no cover
    # 离线 / 直接脚本 import 路径(``from ...`` 相对 import 失败):从根
    # 目录拿 resolve_ref,但要赋一个可调用的名字给 ``_resolve`` 用。
    import types as _types
    _framework_discovery = _types.SimpleNamespace()
    from discovery import resolve_ref as _framework_resolve_ref  # noqa: F401
    _framework_discovery.resolve_ref = _framework_resolve_ref  # type: ignore[attr-defined]
    from modules.Conveyor.runtime import ConveyorModule  # noqa: F401
    from modules.Conveyor.naming import is_host  # noqa: F401


FLAG = "_conveyor_discovered"


def _resolve(cfg, field: str):
    """走 framework 的 resolve_ref,统一处理 _PropertyDeferred 包装。"""
    try:
        return _framework_discovery.resolve_ref(cfg, field)
    except Exception:
        return None


def auto_fill(host) -> bool:
    """校验所有必填 cfg pointer 都已设置。"""
    cfg = getattr(host, "conveyor", None)
    if cfg is None:
        return False
    for field in ("drive_roller", "idler_roller", "belt"):
        if _resolve(cfg, field) is None:
            return False
    return True


def build(host, collision_engine=None, scene=None) -> Optional[ConveyorModule]:
    """构造 :class:`ConveyorModule`,失败返回 None。"""
    cfg = getattr(host, "conveyor", None)
    if cfg is None:
        return None
    drive = _resolve(cfg, "drive_roller")
    idler = _resolve(cfg, "idler_roller")
    belt = _resolve(cfg, "belt")
    if not all((drive, idler, belt)):
        return None
    if drive is idler:
        print(
            f"[Conveyor] {host.name}: drive_roller and idler_roller "
            f"must be distinct objects."
        )
        return None
    try:
        return ConveyorModule(
            host_obj=host,
            drive_roller=drive,
            idler_roller=idler,
            belt=belt,
            collision_engine=collision_engine,
            module_id=host.name,
            scene=scene,
        )
    except ValueError as exc:
        print(f"[Conveyor] {host.name}: {exc}")
        return None
    except Exception as exc:
        print(f"[Conveyor] {host.name}: build failed: {exc!r}")
        return None


def discover(host, collision_engine=None, scene=None) -> Optional[ConveyorModule]:
    """Conveyor host 的发现入口。"""
    cfg = getattr(host, "conveyor", None)
    if cfg is None or not cfg.enabled:
        return None
    if not auto_fill(host):
        missing = []
        for field in ("drive_roller", "idler_roller", "belt"):
            if _resolve(cfg, field) is None:
                missing.append(field)
        print(
            f"[Conveyor] {host.name}: missing required cfg pointer(s): "
            f"{', '.join(missing) or '<unknown>'}. Wire them in the Object "
            f"Properties -> Conveyor panel."
        )
        return None
    return build(host, collision_engine=collision_engine, scene=scene)


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