"""ApproachSensor discovery: cfg 校验 + runtime 构造。

参照 :mod:`modules.Cylinder.discovery` / :mod:`modules.VacuumNozzle.discovery`
的契约:

- :data:`FLAG`  ——  已发现标志位 (``_approach_sensor_discovered``)
- :func:`is_host` —— 由 :mod:`modules.ApproachSensor.naming` 提供
- :func:`auto_fill` —— 校验 PropertyGroup 是否存在 + host 是 mesh
- :func:`discover` —— 构造 :class:`ApproachSensorModule`, 失败返回 ``None``
- :func:`clear_flag` —— 清标志

校验要点
--------
1. ``host.approach_sensor.enabled`` 必须为 True (与 LinearAxis / Cylinder
   同构按 enabled 门控 —— ApproachSensor 默认 enabled=True, 但用户在面板
   勾掉后才会被发现; 为避免"先 enable 插件再勾上 sensor"路径断掉, 与
   VacuumNozzle 一样**不**按 enabled 门控过滤, 保持发现一次到位,
   关掉走 runtime 的 STATE_DISABLED 路径)。
2. host 必须是 **MESH** 类型 (感应区几何以 mesh 局部系为参考,
   没有 mesh 就没法做 SAT; EMPTY 在 build 阶段 print 提示并跳过)。
3. host 上 ``sensor_type`` 必须是 ``"approach"`` (由
   :class:`components.approach_sensor.ApproachSensor` 构造时写,
   或由 artist 在 Object Properties → Custom Properties 手写)。
4. 没有必填 pointer —— ApproachSensor module **不**需要 ``trigger_obj``
   pointer, 扫描候选由 runtime 在每 tick 自动跑。
"""

from __future__ import annotations

from typing import Optional

try:
    from .runtime import ApproachSensorModule
    from .naming import is_host
except ImportError:  # pragma: no cover
    from modules.ApproachSensor.runtime import ApproachSensorModule  # noqa: F401
    from modules.ApproachSensor.naming import is_host  # noqa: F401


FLAG = "_approach_sensor_discovered"


def _sensor_type_ok(host) -> bool:
    """检查 host 上 ``sensor_type`` custom property 是不是 ``"approach"``。

    没设置 (``None``) → print 提示并跳过 (避免覆盖 artist 已有标记);
    设为其它字符串 → print 提示并跳过。
    """
    try:
        existing = host.get("sensor_type", None)
    except Exception:
        existing = None
    if existing is None:
        try:
            host["sensor_type"] = "approach"
        except Exception:
            pass
        return True
    return existing == "approach"


def auto_fill(host) -> bool:
    """校验 ApproachSensor host 可被构建: PropertyGroup 存在 + host 是 MESH。"""
    cfg = getattr(host, "approach_sensor", None)
    if cfg is None:
        return False
    if getattr(host, "type", None) != "MESH":
        return False
    return True


def build(host, collision_engine=None) -> Optional[ApproachSensorModule]:
    """构造 :class:`ApproachSensorModule`, 失败返回 None。"""
    cfg = getattr(host, "approach_sensor", None)
    if cfg is None:
        return None
    if getattr(host, "type", None) != "MESH":
        print(
            f"[ApproachSensor] {host.name}: host must be a MESH object (感应区 "
            f"几何以 mesh 局部系为参考, 没有 mesh 无法做 SAT)。"
        )
        return None
    if not _sensor_type_ok(host):
        print(
            f"[ApproachSensor] {host.name}: sensor_type must be 'approach' "
            f"(found {host.get('sensor_type', None)!r})."
        )
        return None
    try:
        return ApproachSensorModule(
            host_obj=host,
            collision_engine=collision_engine,
            module_id=host.name,
        )
    except Exception as exc:
        print(f"[ApproachSensor] {host.name}: build failed: {exc!r}")
        return None


def discover(host, collision_engine=None) -> Optional[ApproachSensorModule]:
    """ApproachSensor host 的发现入口。

    按 ``cfg.enabled`` 门控 —— 与 LinearAxis / Cylinder 同构。
    ApproachSensorProperty 默认 ``enabled=True``, 所以"先 enable 插件再勾上
    sensor"的路径天然成立; artist 想关掉某个 sensor 时取消勾选 + 点
    Refresh, sensor 模块会从 manager 移除。
    """
    cfg = getattr(host, "approach_sensor", None)
    if cfg is None:
        return None
    if not cfg.enabled:
        return None
    if not auto_fill(host):
        return None
    return build(host, collision_engine=collision_engine)


def clear_flag(host) -> int:
    """Drop the :data:`FLAG` from ``host``. Returns 1 if it was present."""
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