"""LinearAxis operator convenience helpers.

These helpers are the MCP ``execute_code`` entry point. They look up
the live :class:`SimulationManager` (singleton, set up by
:mod:`LinearAxis.addon`) and dispatch through it rather than touching
custom properties directly.

Public API
----------
- :func:`find_slider(axis_id)` -> slider ``bpy`` object, or raises
  :class:`KeyError`.
- :func:`read_state(axis_id)` -> dict snapshot of all ``axis_*`` props.
- :func:`send_command(axis_id, action, ...)` -> int (new ``axis_cmd_seq``).
  Bypasses the singleton by writing ``axis_cmd_*`` on the slider so
  external callers (C# over a future bridge, raw MCP scripts) can
  drive the axis without importing the Python API.
- :func:`refresh_axes()` -> int. Drop and rebuild the discovered axes.

These helpers do not register Blender operators (``bpy.ops.*``); they
are plain functions so we avoid the Operator-class boilerplate and the
context-restriction pitfalls that come with it.
"""

from __future__ import annotations

import json
from typing import Optional

try:
    from ...addon import get_manager
    from .axis import VALID_ACTIONS
except ImportError:  # pragma: no cover - offline / direct-script import path
    from addon import get_manager
    from modules.LinearAxix.axis import VALID_ACTIONS


_VALID_ACTIONS_SET = frozenset(VALID_ACTIONS)


# AxisBlockedError lives in :mod:`modules.axis_errors` so both
# LinearAxis and RotateAxis share one definition. Re-exported here
# so ``from .axis_ops import AxisBlockedError`` keeps working.
from ..axis_errors import AxisBlockedError  # noqa: F401


from ._units import scene_linear_mm_to_bu_factor as _scene_linear_mm_to_bu_factor


def _require_axis(axis_id: str):
    """Return the :class:`LinearAxis` for ``axis_id`` or raise ``KeyError``.

    Uses :meth:`SimulationManager.get_module_by_kind` so a
    ``LinearAxis1`` and a ``RotateAxis1`` (both with
    ``module_id == "1"``) are dispatched correctly — a
    ``send_command("1", ...)`` from the LinearAxis panel always lands
    on the LinearAxis, never on the rotation axis with the same id.
    """
    manager = get_manager()
    if manager is None:
        raise KeyError(f"LinearAxis addon not registered (axis_id={axis_id!r})")
    get = getattr(manager, "get_module_by_kind", None)
    if get is not None:
        axis = get("linear_axis", axis_id)
    else:
        axis = manager.get(axis_id)
    if axis is None:
        try:
            refresh_axes()
        except Exception:
            pass
        if get is not None:
            axis = get("linear_axis", axis_id)
        else:
            axis = manager.get(axis_id)
    if axis is None:
        raise KeyError(f"No LinearAxis with axis_id={axis_id!r}")
    if getattr(axis, "kind", None) != "linear_axis":
        raise KeyError(
            f"axis_id={axis_id!r} is a {getattr(axis, 'kind', '?')!r}, "
            f"not a linear_axis"
        )
    return axis


def find_slider(axis_id: str):
    """Return the Slider Blender object for ``axis_id``.

    Raises :class:`KeyError` if the axis is not registered so callers
    can branch on it.
    """
    return _require_axis(axis_id).slider.obj


def read_state(axis_id: str) -> dict:
    """Return a dict snapshot of an axis's state.

    Pulls every ``axis_*`` custom property off the Slider, decodes the
    ``axis_sensors`` JSON, and returns a plain ``dict`` so MCP
    ``execute_code`` can serialise it without further work.
    """
    axis = _require_axis(axis_id)
    obj = axis.slider.obj
    snapshot: dict = {}
    for key in obj.keys():
        if not key.startswith("axis_"):
            continue
        val = obj[key]
        if key == "axis_sensors" and isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                pass
        snapshot[key] = val
    snapshot["axis_id"] = axis.axis_id
    snapshot["slider_name"] = obj.name
    return snapshot


def send_command(axis_id: str, action: str,
                 *, target_x: Optional[float] = None,
                 target_angle: Optional[float] = None,
                 velocity: Optional[float] = None,
                 duration_s: Optional[float] = None,
                 home_direction: Optional[int] = None) -> int:
    """Write a command onto the Slider's custom properties.

    Always increments ``axis_cmd_seq`` so the axis consumes the
    command on the next tick. Returns the new ``axis_cmd_seq`` value
    so callers can correlate.

    Parameters
    ----------
    axis_id : str
        The axis identifier (the suffix of the host Empty's name).
    action : str
        One of ``"idle"``, ``"stop"``, ``"home"``, ``"move_to"``,
        ``"velocity"``, ``"reset_collision"``.
    target_x : float, optional
        P2P target for LinearAxis (``move_to``).
    target_angle : float, optional
        P2P target for RotateAxis (``move_to``). Both are accepted so
        the same function works for either module — only the relevant
        field is written.
    velocity, duration_s, home_direction : optional
        Only the parameters relevant to ``action`` are written.

    单位约定：LinearAxis 的 ``target_x`` / ``velocity`` 按 **mm**
    输入，由本函数换算成 BU 后写入 ``axis_cmd_*``。换算系数来自
    当前场景的 ``unit_settings.scale_length``（m / BU）：
    ``BU = mm * 0.001 / scale_length``。

    因此调用方按 mm / mm·s⁻¹ 传值即可，无论场景实际是 1 BU = 1 mm
    还是 1 BU = 1 m。RotateAxis 的角度字段（degrees）不涉及此换算。
    """
    if action not in _VALID_ACTIONS_SET:
        raise ValueError(
            f"Unknown action {action!r}; expected one of {sorted(_VALID_ACTIONS_SET)}"
        )
    axis = _require_axis(axis_id)
    # BLOCKED 期间拒绝所有 motion 命令。 ``reset_collision`` 不是
    # axis_cmd_* 协议里的 action(它走 :meth:`CollisionEngine.clear_collision`
    # 或 ``set_disabled``),所以这里没有“除 reset_collision 外”的例外:
    # 任何 action 都被拒绝。
    # 原本 ``_consume_cmd_blocked`` 会静默推进 ``_last_seq`` 并丢掉命令,
    # UI / RPC 拿不到反馈,slider 看似“跳到莫名其妙的地方”。提前在发送点
    # fail-fast:UI operator 报 ERROR + 返回 CANCELLED;RPC 收到一个可识别的
    # 异常后会在状态码里区分。
    if getattr(axis, "state", None) == "blocked":
        raise AxisBlockedError(
            f"LinearAxis {axis_id!r} is blocked by collision; "
            f"call CollisionEngine.clear_collision() to clear the block."
        )
    obj = axis.slider.obj
    obj["axis_cmd_action"] = action
    if target_x is not None:
        obj["axis_cmd_target_x"] = float(target_x) * _scene_linear_mm_to_bu_factor()
    if target_angle is not None:
        obj["axis_cmd_target_angle"] = float(target_angle)
    if velocity is not None:
        obj["axis_cmd_velocity"] = float(velocity) * _scene_linear_mm_to_bu_factor()
    if duration_s is not None:
        obj["axis_cmd_duration_s"] = float(duration_s)
    if home_direction is not None:
        obj["axis_home_direction"] = int(home_direction)
    new_seq = int(obj.get("axis_cmd_seq", 0)) + 1
    obj["axis_cmd_seq"] = new_seq
    return new_seq


def refresh_axes() -> int:
    """Drop every registered axis and re-run discovery from the scene.

    Returns the number of axes newly registered. Useful after editing
    the scene (renaming parts, reparenting, etc.) without restarting
    the addon.
    """
    import bpy
    try:
        from ... import discovery
    except ImportError:  # pragma: no cover - offline / direct-script import path
        import discovery

    manager = get_manager()
    if manager is None:
        raise RuntimeError("LinearAxis addon not registered")

    scene = bpy.context.scene
    if scene is None:
        return 0

    # Unregister every LinearAxis currently registered. LinearAxis is
    # the only module kind that exposes ``axis_id`` (CylinderModule /
    # RotateAxisRuntime expose ``module_id`` instead), so the loop must
    # filter by ``kind`` rather than blindly dereferencing
    # ``axis.axis_id``. Otherwise a CylinderModule in the manager's
    # roster would raise ``AttributeError: 'CylinderModule' object has
    # no attribute 'axis_id'`` and break Refresh for the whole
    # operator — see the dev_panel error report.
    for axis in list(manager.all()):
        if getattr(axis, "kind", None) != "linear_axis":
            continue
        axis_id = getattr(axis, "axis_id", None) or getattr(axis, "module_id", None)
        if axis_id is None:
            continue
        manager.unregister(axis_id)
    # Drop the discovered flags so the next pass re-evaluates auto-fill.
    discovery.clear_discovered_flags(scene)
    # Re-discover.
    return discovery.discover_and_register(scene, manager)