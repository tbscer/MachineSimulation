"""RotateAxis discovery: cfg pointer validation + runtime construction.

This is the RotateAxis half of the two-part discovery design. The
framework entry (``discovery.py`` at the package root) walks the
scene once and delegates every ``RotateAxis*`` EMPTY host to
:func:`discover` here.

For each host:

1. Skip if ``host.rotate_axis`` is missing or ``enabled`` is False.
2. Verify the **required** cfg pointer fields are set
   (``rotate_center`` + ``rotator``). Optional fields
   (``trigger_shim`` + sensors) are skipped silently if unset.
3. Construct a passive :class:`RotateAxisRuntime` and return it; the
   entry registers it with the :class:`SimulationManager` and stamps
   the :data:`FLAG`.

Naming convention removal
-------------------------
旧版 ``auto_fill`` 会按 ``RotateCenter`` / ``RotateShaft`` /
``Rotator`` / ``Sensor_Home`` 等 alias 名字从 host 的 children 里**猜**
出哪个是 rotation center / rotator / sensor，再写回 cfg pointer。这层
“猜”隐式地把 plugin 跟 mesh 名字耦合 —— 美术改名 / 复制 / 用不同前缀
时 plugin 直接跳过、面板 Home 按钮报 “找不到轴 ID”。彻底删除：runtime
拿到 host 之后只看 cfg pointer，没填就跳过；命名约定仅用于 host 本体
的识别（host 必须以 ``RotateAxis`` 为前缀），见
:mod:`modules.RotateAxis.naming`。

Sensor type (``home`` / ``pos_limit`` / ``neg_limit``) 也不再从 mesh
名字解析 —— ``KIND_TO_FIELD`` 把 cfg 字段名直接映射到 sensor kind：

    rotate_home_sensor        -> home
    rotate_pos_limit_sensor   -> pos_limit
    rotate_neg_limit_sensor   -> neg_limit

只要美术在 Object Properties → ``Rotate Axis`` 面板里把对应 pointer
指到任一 mesh，runtime 就把它当 home / pos / neg sensor。

Discoverer contract used by the framework entry:
``FLAG`` / ``is_host`` / ``discover`` / ``clear_flag``.
"""

from __future__ import annotations

from typing import List, Optional

try:
    from ...discovery import resolve_ref
    from .rotate_axis import RotateAxisRuntime
    from .naming import is_host
    from .rotate_center import RotateCenterComponent
    from .rotator import RotateRotatorComponent
    from ..components.sensor import UTypeSensor
    from ..components.trigger_shim import TriggerShimComponent
except ImportError:  # pragma: no cover - offline / direct-script import path
    from discovery import resolve_ref  # noqa: F401
    from modules.RotateAxis.rotate_axis import RotateAxisRuntime  # noqa: F401
    from modules.RotateAxis.naming import is_host  # noqa: F401
    from modules.RotateAxis.rotate_center import RotateCenterComponent  # noqa: F401
    from modules.RotateAxis.rotator import RotateRotatorComponent  # noqa: F401
    from modules.components.sensor import UTypeSensor  # noqa: F401
    from modules.components.trigger_shim import TriggerShimComponent  # noqa: F401


# Custom-property flag stamped on a host once it has been discovered.
FLAG = "_rotate_axis_discovered"


# PropertyGroup field names for the rotate-axis-specific Object refs.
# Mirrors the LinearAxisProperty shape so a single PropertyGroup can
# serve hosts of either kind. Field names match what they hold:
# ``rotate_center`` is the static pivot, ``rotator`` is the orbiting
# body.
ROTATE_CENTER_FIELD = "rotate_center"
ROTATOR_FIELD = "rotator"
TRIGGER_SHIM_FIELD = "trigger_shim"

# Sensor kind -> PropertyGroup field name on the host. Sensor kind is
# derived from the cfg **field name**, not from the mesh's name — this
# keeps runtime independent of mesh naming conventions.
KIND_TO_FIELD = {
    "home":      "rotate_home_sensor",
    "pos_limit": "rotate_pos_limit_sensor",
    "neg_limit": "rotate_neg_limit_sensor",
}


# Default axis index when nothing else applies. Mirrors the
# ``RotateAxisProperty.rotate_axis_index`` default so the "user left
# it alone" heuristic in :func:`build_axis` is reliable.
DEFAULT_AXIS_INDEX = 2

_AXIS_LABELS = ("X", "Y", "Z")


def _detect_rotate_axis(obj) -> int:
    """Return the local axis index (0=X, 1=Y, 2=Z) whose bound_box
    extent is largest — i.e. the cylinder/shaft's length axis. Falls
    back to :data:`DEFAULT_AXIS_INDEX` (``2`` / Z) on any error.

    The detection uses the object's **local** ``bound_box`` (i.e.
    before applying ``matrix_world``) because the rotation pivot lives
    on the centre's own mesh: we want to know which of the centre's
    *local* X/Y/Z aligns with its length, not which world axis.
    """
    try:
        bb = getattr(obj, "bound_box", None)
        if bb is None or len(bb) < 8:
            return DEFAULT_AXIS_INDEX
        extents = [
            max(c[0] for c in bb) - min(c[0] for c in bb),  # local X
            max(c[1] for c in bb) - min(c[1] for c in bb),  # local Y
            max(c[2] for c in bb) - min(c[2] for c in bb),  # local Z
        ]
        return extents.index(max(extents))
    except Exception:
        return DEFAULT_AXIS_INDEX


def detect_rotate_axis(cfg, host) -> int:
    """Public axis-detection helper used by the ``Detect Axis`` button.

    Reads the rotate centre off the cfg (no naming fallback), runs the
    local-AABB heuristic, writes the result into
    ``cfg.rotate_axis_index``, and returns the chosen index. The cfg
    mutation is only committed if it has a settable attribute (real
    Blender PropertyGroup) — tests can call this against a plain
    mock and it still returns the value.
    """
    center_obj = resolve_ref(cfg, ROTATE_CENTER_FIELD)
    if center_obj is None:
        # 没配 rotation center → UI 按钮“无目标可检测”。返回默认值，
        # 不再做命名 fallback（命名匹配已彻底删除）。
        return DEFAULT_AXIS_INDEX
    detected = _detect_rotate_axis(center_obj)
    try:
        cfg.rotate_axis_index = detected
    except Exception:
        pass
    return detected


# ---- cfg pointer resolution ----

def _resolve_or_lookup(cfg, host, field: str):
    """Pure cfg pointer resolver.

    Returns ``cfg.<field>`` directly. No naming-based fallback: the
    artist must wire the pointer in the Object Properties panel. The
    ``host`` argument is kept only for API symmetry with previous
    versions and to support future "smart fallback" policies; right
    now it's unused.
    """
    return resolve_ref(cfg, field)


# ---- auto-fill (now: cfg validation) ----

def auto_fill(host) -> bool:
    """Validate that the host's required cfg pointers are set.

    Returns True iff both required fields (``rotate_center`` +
    ``rotator``) point to live Blender Objects. Optional fields
    (``trigger_shim`` + sensors) are skipped silently if unset.

    旧版的 ``auto_fill`` 会按命名约定查找孩子并写入 cfg —— 那段已经删除。
    现在如果美术没在 panel 里手动配 ``rotate_center`` 和 ``rotator``，这个
    函数会返回 False，discoverer 跳过这个 host（``discover```` 会提示
    “missing required cfg pointers”）。
    """
    cfg = getattr(host, "rotate_axis", None)
    if cfg is None:
        return False
    if resolve_ref(cfg, ROTATE_CENTER_FIELD) is None:
        return False
    if resolve_ref(cfg, ROTATOR_FIELD) is None:
        return False
    return True


# ---- axis construction ----

def build_axis(host, collision_engine=None) -> Optional[RotateAxisRuntime]:
    """Construct a passive :class:`RotateAxisRuntime` from the host.

    Returns ``None`` if the host has no PropertyGroup or required refs
    are missing. ``collision_engine`` is passed through to the runtime.
    """
    cfg = getattr(host, "rotate_axis", None)
    if cfg is None:
        return None

    center_obj = _resolve_or_lookup(cfg, host, ROTATE_CENTER_FIELD)
    rotator_obj = _resolve_or_lookup(cfg, host, ROTATOR_FIELD)
    if center_obj is None or rotator_obj is None:
        return None

    # Parenting between rotator and rotate center is intentionally NOT
    # enforced here. The runtime rotates the rotator around the
    # rotate center's world pivot directly, so the rig is free to use
    # whatever scene graph the artist prefers.

    angular_speed_raw = resolve_ref(cfg, "angular_speed")
    angular_home_speed_raw = resolve_ref(cfg, "angular_home_speed")
    default_velocity = float(angular_speed_raw) if isinstance(angular_speed_raw, (int, float)) else 0.5
    home_velocity = float(angular_home_speed_raw) if isinstance(angular_home_speed_raw, (int, float)) else 0.1

    # Auto-detect the rotation axis only when the user has left it at
    # the default value (2). An explicit 0 or 1 means the user has
    # configured the rig and we respect their choice.
    axis_idx_raw = resolve_ref(cfg, "rotate_axis_index")
    if isinstance(axis_idx_raw, (int, float)) and int(axis_idx_raw) == DEFAULT_AXIS_INDEX:
        detected = _detect_rotate_axis(center_obj)
        if detected != DEFAULT_AXIS_INDEX:
            try:
                cfg.rotate_axis_index = detected
            except Exception:
                pass
            print(
                f"[RotateAxis][{host.name}] auto-detected "
                f"rotate_axis_index = {detected} "
                f"({_AXIS_LABELS[detected]}) from centre mesh"
            )

    center = RotateCenterComponent(center_obj)
    rotator = RotateRotatorComponent(
        rotator_obj,
        center=center,
        default_velocity=default_velocity,
        home_velocity=home_velocity,
    )
    # Sensors: read each kind's cfg pointer directly. The sensor's
    # `kind` comes from the **cfg field name** (see KIND_TO_FIELD),
    # not from any naming convention on the mesh itself.
    sensors: List[UTypeSensor] = []
    for kind, field in KIND_TO_FIELD.items():
        ptr = _resolve_or_lookup(cfg, host, field)
        if ptr is not None:
            sensors.append(UTypeSensor(ptr, kind=kind))
    shim_obj = _resolve_or_lookup(cfg, host, TRIGGER_SHIM_FIELD)
    shim = (
        TriggerShimComponent(shim_obj, rotator_obj)
        if shim_obj is not None
        else None
    )
    if shim is not None:
        try:
            shim.validate()
        except ValueError as exc:
            print(f"[RotateAxis][{host.name}] {exc}; shim ignored")
            shim = None
    return RotateAxisRuntime(
        host_obj=host,
        center=center,
        rotator=rotator,
        sensors=sensors,
        shim=shim,
        collision_engine=collision_engine,
    )


# ---- discoverer contract ----

def discover(host, collision_engine=None) -> Optional[RotateAxisRuntime]:
    """Build the RotateAxisRuntime for ``host``, or ``None`` to skip.

    Skips hosts without a PropertyGroup, disabled hosts, and hosts
    whose required cfg pointers (``rotate_center`` + ``rotator``) are
    not set (with a warning telling the artist which panel field is
    missing).

    ``collision_engine`` is forwarded to the runtime so the per-tick
    BVH check is active from the very first frame. ``None`` is
    accepted (collision checks skipped) for offline tests and for any
    caller that has not yet installed the engine.
    """
    cfg = getattr(host, "rotate_axis", None)
    if cfg is None or not cfg.enabled:
        return None
    if not auto_fill(host):
        # 提示具体哪个必填 pointer 没填，引导美术去 Object Properties
        # → ``Rotate Axis`` 面板里关联，而不是靠“命名约定”去猜。
        missing = []
        if resolve_ref(cfg, ROTATE_CENTER_FIELD) is None:
            missing.append("rotate_center")
        if resolve_ref(cfg, ROTATOR_FIELD) is None:
            missing.append("rotator")
        print(
            f"[RotateAxis] {host.name}: missing required cfg pointer(s): "
            f"{', '.join(missing) or '<unknown>'}. "
            f"Wire them in the Object Properties → Rotate Axis panel."
        )
        return None
    return build_axis(host, collision_engine=collision_engine)


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
    "ROTATE_CENTER_FIELD",
    "ROTATOR_FIELD",
    "TRIGGER_SHIM_FIELD",
    "KIND_TO_FIELD",
    "DEFAULT_AXIS_INDEX",
    "_detect_rotate_axis",
    "detect_rotate_axis",
    "auto_fill",
    "build_axis",
    "discover",
    "clear_flag",
]