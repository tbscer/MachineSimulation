# -*- coding: utf-8 -*-
"""Cylinder discovery:cfg pointer 校验 + runtime 构造。

参照 :mod:`modules.LinearAxix.discovery` 的契约:

- :data:`FLAG`  ——  已发现标志位 (``_cylinder_discovered``)
- :func:`is_host` —— 由 :mod:`modules.Cylinder.naming` 提供
- :func:`discover` —— 构造 :class:`CylinderModule`,失败返回 ``None``
- :func:`clear_flag` —— 清标志

校验要点
--------
1. ``host.cylinder.enabled`` 必须为 True。
2. 必填 pointer:``work_bar`` / ``touch_shim`` / ``approach_sensor_1`` /
   ``approach_sensor_2``。缺失则在控制台打印提示,跳过。
3. 两 approach sensor 不能是同一个 Object(否则 axis_dir 无定义)。
4. approach sensor 物体上 ``sensor_type`` 若已有值,接受 ``"approach"``;
   若未设置或为其它值,打印提示并跳过(避免覆盖 artist 已有标记)。
5. :class:`CylinderModule` 构造时还会校验两 sensor 世界位置不重合
   (否则推导 axis_dir 会 raise)。
"""

from __future__ import annotations

from typing import Optional

try:
    from ... import discovery as _framework_discovery
    from .cylinder import CylinderModule
    from .naming import is_host
except ImportError:  # pragma: no cover
    from discovery import resolve_ref as _framework_resolve_ref  # noqa: F401
    from modules.Cylinder.cylinder import CylinderModule  # noqa: F401
    from modules.Cylinder.naming import is_host  # noqa: F401


FLAG = "_cylinder_discovered"


def _resolve(cfg, field: str):
    """走 framework 的 resolve_ref,统一处理 _PropertyDeferred 包装。"""
    try:
        return _framework_discovery.resolve_ref(cfg, field)
    except Exception:
        return None


def auto_fill(host) -> bool:
    """校验所有必填 cfg pointer 都已设置。"""
    cfg = getattr(host, "cylinder", None)
    if cfg is None:
        return False
    for field in ("work_bar", "touch_shim", "approach_sensor_1", "approach_sensor_2"):
        if _resolve(cfg, field) is None:
            return False
    return True


def build(host, collision_engine=None) -> Optional[CylinderModule]:
    """构造 :class:`CylinderModule`,失败返回 None。"""
    cfg = getattr(host, "cylinder", None)
    if cfg is None:
        return None
    work_bar = _resolve(cfg, "work_bar")
    touch_shim = _resolve(cfg, "touch_shim")
    sensor_1 = _resolve(cfg, "approach_sensor_1")
    sensor_2 = _resolve(cfg, "approach_sensor_2")
    if not all((work_bar, touch_shim, sensor_1, sensor_2)):
        return None
    if sensor_1 is sensor_2:
        print(
            f"[Cylinder] {host.name}: approach_sensor_1 and approach_sensor_2 "
            f"must be distinct objects."
        )
        return None
    # approach sensor 的 sensor_type 必须是 approach
    for s, label in ((sensor_1, "approach_sensor_1"), (sensor_2, "approach_sensor_2")):
        try:
            existing = s.get("sensor_type", None)
        except Exception:
            existing = None
        if existing is None:
            try:
                s["sensor_type"] = "approach"
            except Exception:
                pass
        elif existing != "approach":
            print(
                f"[Cylinder] {host.name}: {label} ({s.name!r}) already has "
                f"sensor_type={existing!r} (not 'approach'). Skipping to avoid "
                f"overwriting user's existing sensor configuration."
            )
            return None
    try:
        return CylinderModule(
            host_obj=host,
            work_bar=work_bar,
            touch_shim=touch_shim,
            approach_sensor_1=sensor_1,
            approach_sensor_2=sensor_2,
            collision_engine=collision_engine,
            module_id=host.name,
        )
    except ValueError as exc:
        print(f"[Cylinder] {host.name}: {exc}")
        return None
    except Exception as exc:
        print(f"[Cylinder] {host.name}: build failed: {exc!r}")
        return None


def discover(host, collision_engine=None) -> Optional[CylinderModule]:
    """Cylinder host 的发现入口。"""
    cfg = getattr(host, "cylinder", None)
    if cfg is None or not cfg.enabled:
        return None
    if not auto_fill(host):
        missing = []
        for field in (
            "work_bar", "touch_shim", "approach_sensor_1", "approach_sensor_2"
        ):
            if _resolve(cfg, field) is None:
                missing.append(field)
        print(
            f"[Cylinder] {host.name}: missing required cfg pointer(s): "
            f"{', '.join(missing) or '<unknown>'}. Wire them in the Object "
            f"Properties -> Cylinder panel."
        )
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