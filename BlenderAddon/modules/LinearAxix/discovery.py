"""LinearAxis discovery: cfg pointer validation + runtime construction.

This is the LinearAxis half of the two-part discovery design. The
framework entry (top-level ``discovery.py``) walks the scene once and
delegates every ``LinearAxis*`` EMPTY host to :func:`discover` here.

For each host:

1. Skip if ``host.linear_axis`` is missing or ``enabled`` is False.
2. Verify the **required** cfg pointer fields are set
   (``slider`` + ``rail``). Optional fields (``trigger_shim`` +
   sensors) are skipped silently if unset.
3. Construct a passive :class:`LinearAxis` and return it; the entry
   registers it with the :class:`SimulationManager` and stamps the
   :data:`FLAG`.

Naming convention removal
-------------------------
旧版 ``auto_fill`` 会按 ``Slider`` / ``Rail`` / ``Sensor_Home`` 等 alias
名字从 host children 里**猜**出哪个是 slider / rail / sensor，再写回
cfg pointer。彻底删除：runtime 完全依赖 cfg pointer（用户在 panel 里
手动关联的 ``PointerProperty``）；命名匹配不再存在。Host 本身仍需按
``LinearAxis`` 前缀 + 非空 id 识别，见 :mod:`modules.LinearAxix.naming`。

Sensor type (``home`` / ``back_limit`` / ``front_limit``) 也不再从
mesh 名字解析 —— ``KIND_TO_FIELD`` 把 cfg 字段名直接映射到 sensor kind：

    home_sensor    -> home
    pos_sensor     -> back_limit
    neg_sensor     -> front_limit

Discoverer contract used by the framework entry:
``FLAG`` / ``is_host`` / ``discover`` / ``clear_flag``.
"""

from __future__ import annotations

from typing import List, Optional

try:
    from ...discovery import resolve_ref
    from .axis import LinearAxis
    from .naming import is_host
    from .rail import RailComponent
    from ..components.sensor import UTypeSensor
    from .slider import SliderComponent
    from ..components.trigger_shim import TriggerShimComponent
except ImportError:  # pragma: no cover - offline / direct-script import path
    from discovery import resolve_ref  # noqa: F401
    from modules.LinearAxix.axis import LinearAxis  # noqa: F401
    from modules.LinearAxix.naming import is_host  # noqa: F401
    from modules.LinearAxix.rail import RailComponent  # noqa: F401
    from modules.components.sensor import UTypeSensor  # noqa: F401
    from modules.LinearAxix.slider import SliderComponent  # noqa: F401
    from modules.components.trigger_shim import TriggerShimComponent  # noqa: F401


# Custom-property flag stamped on a host once it has been discovered.
FLAG = "_linear_axis_discovered"


# Sensor kind -> PropertyGroup field name on the host. Sensor kind is
# derived from the cfg **field name**, not from the mesh's name.
KIND_TO_FIELD = {
    "home":        "home_sensor",
    "back_limit":  "pos_sensor",
    "front_limit": "neg_sensor",
}


# ---- cfg pointer resolution ----

def _resolve_or_lookup(cfg, host, field: str, kind_to_name: Optional[dict] = None):
    """Pure cfg pointer resolver.

    Returns ``cfg.<field>`` directly. No naming-based fallback: the
    artist must wire the pointer in the Object Properties panel. The
    ``host`` and ``kind_to_name`` arguments are kept only for API
    symmetry with previous versions; right now they're unused.
    """
    return resolve_ref(cfg, field)


# ---- auto-fill (now: cfg validation) ----

def auto_fill(host) -> bool:
    """Validate that the host's required cfg pointers are set.

    Returns True iff both required fields (``slider`` + ``rail``) point
    to live Blender Objects. Optional fields (``trigger_shim`` +
    sensors) are skipped silently if unset.

    旧版的 ``auto_fill`` 会按命名约定查找孩子并写入 cfg —— 那段已经删除。
    现在如果美术没在 panel 里手动配 ``slider`` 和 ``rail``，这个函数会
    返回 False，discoverer 跳过这个 host（``discover`` 会提示缺失的具体
    pointer 名，引导美术去面板里配）。
    """
    cfg = getattr(host, "linear_axis", None)
    if cfg is None:
        return False
    if resolve_ref(cfg, "slider") is None:
        return False
    if resolve_ref(cfg, "rail") is None:
        return False
    return True


# ---- axis construction ----

def build_axis(host, collision_engine=None) -> Optional[LinearAxis]:
    """Construct a passive :class:`LinearAxis` from the host's PropertyGroup.

    Returns ``None`` if the host has no PropertyGroup or required refs
    are missing. ``collision_engine`` is passed through to the runtime.

    FloatProperty fields may be deferred in some Blender contexts; we
    treat any non-numeric value as "use the default".
    """
    cfg = getattr(host, "linear_axis", None)
    if cfg is None:
        return None

    slider_obj = _resolve_or_lookup(cfg, host, "slider")
    rail_obj   = _resolve_or_lookup(cfg, host, "rail")
    if slider_obj is None or rail_obj is None:
        return None

    speed_raw = resolve_ref(cfg, "speed")
    home_speed_raw = resolve_ref(cfg, "home_speed")
    default_velocity = float(speed_raw) if isinstance(speed_raw, (int, float)) else 0.5
    home_velocity = float(home_speed_raw) if isinstance(home_speed_raw, (int, float)) else 0.1

    rail = RailComponent(rail_obj)
    slider = SliderComponent(
        slider_obj,
        rail=rail,
        default_velocity=default_velocity,
        home_velocity=home_velocity,
    )
    # Sensors: read each kind's cfg pointer directly. Sensor `kind` is
    # derived from the cfg **field name** (see KIND_TO_FIELD).
    sensors: List[UTypeSensor] = []
    for kind, field in KIND_TO_FIELD.items():
        ptr = _resolve_or_lookup(cfg, host, field)
        if ptr is not None:
            sensors.append(UTypeSensor(ptr, kind=kind))
    shim_obj = _resolve_or_lookup(cfg, host, "trigger_shim")
    shim = (
        TriggerShimComponent(shim_obj, slider_obj)
        if shim_obj is not None
        else None
    )
    if shim is not None:
        try:
            shim.validate()
        except ValueError as exc:
            print(f"[LinearAxis][{host.name}] {exc}; shim ignored")
            shim = None
    return LinearAxis(
        host_obj=host,
        slider=slider,
        rail=rail,
        sensors=sensors,
        shim=shim,
        collision_engine=collision_engine,
    )


# ---- discoverer contract ----

def discover(host, collision_engine=None) -> Optional[LinearAxis]:
    """Build the LinearAxis runtime for ``host``, or ``None`` to skip.

    Skips hosts without a PropertyGroup, disabled hosts, and hosts
    whose required cfg pointers (``slider`` + ``rail``) are not set
    (with a warning telling the artist which panel field is missing).

    ``collision_engine`` is forwarded to the runtime so the per-tick
    BVH check is active from the very first frame. ``None`` is
    accepted (collision checks skipped) for offline tests and for any
    caller that has not yet installed the engine.
    """
    cfg = getattr(host, "linear_axis", None)
    if cfg is None or not cfg.enabled:
        return None
    if not auto_fill(host):
        missing = []
        if resolve_ref(cfg, "slider") is None:
            missing.append("slider")
        if resolve_ref(cfg, "rail") is None:
            missing.append("rail")
        print(
            f"[LinearAxis] {host.name}: missing required cfg pointer(s): "
            f"{', '.join(missing) or '<unknown>'}. "
            f"Wire them in the Object Properties → Linear Axis panel."
        )
        return None
    return build_axis(host, collision_engine=collision_engine)


def clear_flag(host) -> int:
    """Drop the :data:`FLAG` from ``host``. Returns 1 if it was present.

    Uses ``host.get`` rather than ``in host`` so the function works
    against real Blender objects AND any minimal-mock stand-in that
    does not implement ``__contains__``.
    """
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
    "KIND_TO_FIELD",
    "_resolve_or_lookup",
    "auto_fill",
    "build_axis",
    "discover",
    "clear_flag",
]