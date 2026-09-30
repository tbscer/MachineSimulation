"""JSON-over-TCP RPC server for MotionSimulation.

Auto-started by ``addon.register()`` once the ``SimulationManager`` singleton
exists. Teardown happens in ``addon.unregister()`` *before* the manager is
cleared so no in-flight request is dispatched against a half-torn-down manager.

Public surface
--------------
- ``register(manager)`` — construct + start the singleton server.
- ``unregister()`` — stop the singleton server. Idempotent.
- ``get_server()`` — return the singleton server, or ``None`` if not started.
- ``RPCServer`` — class, exposed so tests can construct isolated instances.
- ``_RpcError`` — exception type raised inside RPC handlers (re-exported so
  tests can construct one without reaching into private state).

Side effects at module import time: none. All work begins at ``register()``.
"""

from __future__ import annotations

from typing import Optional

from .server import RPCServer, _RpcError

_SERVER: Optional["RPCServer"] = None


def register(manager) -> RPCServer:
    """Construct + start the singleton :class:`RPCServer` for ``manager``.

    Idempotent: a second call returns the existing server without restarting.
    """
    global _SERVER
    if _SERVER is not None:
        return _SERVER
    server = RPCServer(manager)
    server.start()
    _SERVER = server
    return server


def unregister() -> None:
    """Stop the singleton server if running. Safe to call twice."""
    global _SERVER
    if _SERVER is None:
        return
    try:
        _SERVER.stop()
    finally:
        _SERVER = None


def get_server() -> Optional[RPCServer]:
    """Return the singleton server, or ``None`` if not started."""
    return _SERVER


__all__ = [
    "RPCServer",
    "_RpcError",
    "register",
    "unregister",
    "get_server",
]