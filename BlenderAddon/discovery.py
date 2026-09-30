"""Unified discovery entry point (framework layer).

Discovery follows the two-part design from the roadmap (第五阶段:
从"发现 LinearAxis"升级为"发现任意模块"):

1. This module is the **entry**. It walks the scene exactly once and
   delegates each candidate host to the discoverer of the module that
   claims it. It also hosts the generic object-binding helpers
   (:func:`find_child`, :func:`resolve_ref`) shared by every module
   discoverer.

2. Each module package implements its **own** discovery in
   ``<module>/discovery.py``, exposing:

   - ``FLAG``             — custom-property flag marking a discovered host
   - ``is_host(obj)``     — True iff ``obj`` is a host of that module
   - ``discover(host, collision_engine=None)``  — build the runtime, or
     ``None`` to skip. The optional ``collision_engine`` is forwarded
     to the module constructor so every axis runtime can perform
     BVH-based collision checks.
   - ``clear_flag(host)`` — drop the discovery flag; returns 1 if present

Adding a new module's discovery = append its discoverer module to
:data:`_DISCOVERERS` (see the built-in registration at the bottom).

Discovery runs **once** at addon enable (deferred past Blender's
``_RestrictData`` window via a zero-delay timer) and not again unless
the user calls ``refresh_axes`` explicitly. There is no per-tick scan.
"""

from __future__ import annotations

from typing import Optional

try:
    from .modules import build_module as _build_module
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules import build_module as _build_module


def build_module(host):
    """Build a runtime module for the given host using the framework factory."""
    return _build_module(host)


# ---- generic binding helpers (shared by module discoverers) ----

def find_child(parent, name: str):
    """Return the first direct child of ``parent`` named ``name``, else None.

    Children are iterated in declaration order. Matches by exact name.
    """
    for c in getattr(parent, "children", ()):
        if c.name == name:
            return c
    return None


def resolve_ref(cfg, field: str):
    """Resolve a PropertyGroup field to a usable real value.

    In some Blender contexts (e.g. MCP ``execute_code``, headless
    scripts) reading a PropertyGroup property returns a
    ``_PropertyDeferred`` wrapper that is not the actual Object / float
    / bool. The wrapper has no useful attribute for our needs; we
    detect it by checking whether the value has the attributes we
    expect, and treat anything that doesn't as "not resolvable". The
    caller then falls back to either the default value or a name-based
    lookup.
    """
    val = getattr(cfg, field, None)
    if val is None:
        return None
    # Real ``bpy.types.Object`` exposes ``bound_box`` + ``matrix_world``.
    if hasattr(val, "bound_box"):
        return val
    # Primitive types come back real even in deferred contexts.
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        return val
    # Anything else (``_PropertyDeferred``, etc.) is unusable.
    return None


# ---- discoverer registry ----
# Discoverer modules are imported at the BOTTOM of this file, after the
# helpers above: module discoverers import these helpers back, and the
# partial-module import only resolves if the names already exist.

_DISCOVERERS: list = []


def register_discoverer(discoverer) -> None:
    """Add a module discoverer to the registry. Idempotent."""
    if discoverer not in _DISCOVERERS:
        _DISCOVERERS.append(discoverer)


def _resolve_collision_engine(manager, explicit) -> Optional[object]:
    """Return the best-available :class:`CollisionEngine` reference.

    Resolution order:
    1. ``explicit`` (caller-supplied, e.g. the addon orchestrator).
    2. ``manager.get_collision_engine()`` (manager-side default).
    3. ``None`` (no engine — modules construct with ``collision_engine=None``
       and skip the per-tick BVH check).
    """
    if explicit is not None:
        return explicit
    if manager is not None and hasattr(manager, "get_collision_engine"):
        try:
            return manager.get_collision_engine()
        except Exception:
            return None
    return None


# ---- one-shot discovery entry ----

def discover_and_register(scene, manager, collision_engine=None) -> int:
    """Walk ``scene.objects`` once, delegating each host to its discoverer.

    For every object, the first discoverer whose ``is_host`` matches
    takes ownership: already-flagged hosts are skipped (idempotent
    rescans), otherwise ``discover(host, collision_engine=engine)``
    builds the runtime module and registers it with ``manager``.
    Hosts claimed by no discoverer are ignored.

    ``collision_engine`` is forwarded to every ``discover`` call so each
    axis runtime can perform BVH-based collision checks. If ``None``,
    the engine is fetched from the manager (set by :mod:`addon` at
    register time); if the manager has no engine either, the
    discoverers build modules without one (collision checks are
    skipped silently — backwards compatible with the pre-collision
    era).

    Returns the number of modules newly registered.
    """
    if scene is None or manager is None:
        return 0
    engine = _resolve_collision_engine(manager, collision_engine)
    count = 0
    for obj in getattr(scene, "objects", ()):
        for d in _DISCOVERERS:
            # 逐项容错:单个 discoverer 抛异常不得拖垮整轮 discovery
            # (线上"注册数为 0"的排查教训:一处异常会静默中断全部注册)。
            try:
                matched = d.is_host(obj)
            except Exception as exc:
                print(
                    "[discovery] %s.is_host(%s) failed: %r"
                    % (getattr(d, "FLAG", "?"), getattr(obj, "name", "?"), exc)
                )
                continue
            if not matched:
                continue
            if obj.get(d.FLAG, False):
                continue
            # 依次尝试新版(collision_engine + scene) → 旧版(scene only)
            # → 最早版(只传 obj)。三层 fallback 让新加 scene 的 conveyor
            # 不破坏老 discoverer 的协议。
            module = None
            try:
                try:
                    module = d.discover(obj, collision_engine=engine, scene=scene)
                except TypeError:
                    try:
                        module = d.discover(obj, scene=scene)
                    except TypeError:
                        try:
                            module = d.discover(obj, collision_engine=engine)
                        except TypeError:
                            module = d.discover(obj)
            except Exception as exc:
                print(
                    "[discovery] %s.discover(%s) failed: %r"
                    % (getattr(d, "FLAG", "?"), getattr(obj, "name", "?"), exc)
                )
                module = None
            if module is None:
                break  # claimed but skipped (disabled / missing refs)
            if hasattr(manager, "register_module"):
                manager.register_module(module)
            else:
                manager.register(module)
            obj[d.FLAG] = True
            count += 1
            break
    return count


def clear_discovered_flags(scene) -> int:
    """Drop discovery flags from every host so the next pass rebuilds.

    Called by ``refresh_axes`` so the next :func:`discover_and_register`
    re-evaluates auto-fill from scratch. Each discoverer clears the
    hosts it claims.
    """
    if scene is None:
        return 0
    n = 0
    for obj in getattr(scene, "objects", ()):
        for d in _DISCOVERERS:
            try:
                matched = d.is_host(obj)
            except Exception:
                continue
            if matched:
                try:
                    n += d.clear_flag(obj)
                except Exception as exc:
                    print(
                        "[discovery] %s.clear_flag(%s) failed: %r"
                        % (getattr(d, "FLAG", "?"), getattr(obj, "name", "?"), exc)
                    )
                break
    return n


# ---- built-in discoverers ----

try:
    from .modules.LinearAxix import discovery as _linear_axis_discovery
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.LinearAxix import discovery as _linear_axis_discovery  # noqa: F401

register_discoverer(_linear_axis_discovery)

try:
    from .modules.RotateAxis import discovery as _rotate_axis_discovery
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.RotateAxis import discovery as _rotate_axis_discovery  # noqa: F401

register_discoverer(_rotate_axis_discovery)

try:
    from .modules.Cylinder import discovery as _cylinder_discovery
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.Cylinder import discovery as _cylinder_discovery  # noqa: F401

register_discoverer(_cylinder_discovery)

try:
    from .modules.VacuumNozzle import discovery as _vacuum_nozzle_discovery
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.VacuumNozzle import discovery as _vacuum_nozzle_discovery  # noqa: F401

register_discoverer(_vacuum_nozzle_discovery)

try:
    from .modules.ApproachSensor import discovery as _approach_sensor_discovery
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.ApproachSensor import discovery as _approach_sensor_discovery  # noqa: F401

register_discoverer(_approach_sensor_discovery)

try:
    from .modules.Conveyor import discovery as _conveyor_discovery
except ImportError:  # pragma: no cover - offline / direct-script import path
    from modules.Conveyor import discovery as _conveyor_discovery  # noqa: F401

register_discoverer(_conveyor_discovery)