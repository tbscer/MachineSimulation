"""Server-side RPC module for MotionSimulation.

Listens on a local TCP socket and forwards JSON-over-newline requests to the
:class:`MotionSimulation.simulation_manager.SimulationManager` singleton. The
protocol is small on purpose: a single general ``apply_command`` plus a small
number of state-subscription and keepalive helpers.

Public methods
--------------
``apply_command(module_id, action, payload)``
    Forwards to ``SimulationManager.apply_command(module_id, SimulationCommand(action, payload))``.
    The exact ``payload`` keys depend on the axis (e.g. ``{"target_x",
    "duration_s"}`` for a linear axis, ``{"target_angle", "duration_s"}`` for a
    rotation axis). See the ``VALID_ACTIONS`` and ``apply_command`` in each
    axis module.

``subscribe_state(interval_ms=100)``
    Begin periodic ``{"type": "state_push", "data": ...}`` pushes of
    ``manager.snapshot()`` to this client. Idempotent. ``interval_ms`` must be
    a positive number.

``unsubscribe_state()``
    Stop the periodic pushes. Idempotent.

``ping()``
    Returns ``{"pong": true}``. Useful as a round-trip / keepalive check.

Response / push framing
-----------------------
All client-to-server messages are newline-delimited JSON objects of the
shape ``{"id": <id>, "method": "...", "params": {...}}``. The server replies
with ``{"id": <id>, "ok": true|false, "result"|"error": ...}``. Push messages
have no ``id`` field; they look like ``{"type": "state_push", "data": {...}}``.

Threading
---------
Only socket I/O happens on worker threads. Every call into the
``SimulationManager`` (or any runtime) is marshalled onto Blender's main
thread via ``bpy.app.timers.register`` so that ``bpy`` data is only ever
touched from the main thread. The push poller is also a ``bpy.app.timers``
callback, which means it is single-threaded with everything else.

A per-client ``send_lock`` (``threading.Lock``) serialises writes to each
client socket so the push poller (main thread) and the response writer
(client thread) cannot interleave bytes.

Security
--------
The server binds to ``127.0.0.1`` only by default. There is no authentication
and no encryption. Do not expose this port outside the local machine.

Framework caveats (relevant to dispatch)
---------------------------------------
- ``SimulationManager.apply_command`` silently no-ops on unknown module ids,
  so the RPC handler explicitly calls ``manager.get(module_id)`` first and
  raises ``AXIS_NOT_FOUND`` when missing. This avoids the surprise of a
  successful ``{"ok": true}`` response that did nothing.
- Multiple axis runtimes may collide on ``module_id`` if their host names
  share the same trailing integer (e.g. ``LinearAxis1`` and ``RotateAxis1``
  both produce ``module_id == "1"``); ``SimulationManager.register_module``
  keeps the first to register. This is a pre-existing framework quirk, not
  introduced by the RPC.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

# Lazy ``bpy`` import inside each method that needs it so that this module
# remains importable for unit-test environments that don't ship Blender's
# Python.
#
# Note: ``rpc/server.py`` is one package below ``MotionSimulation/`` so the
# correct relative path to the framework module is ``..framework`` (parent
# package). An earlier draft used ``.framework`` which would resolve to a
# non-existent ``rpc/framework.py``; do not regress that.
try:
    from ..framework import SimulationCommand
except ImportError:  # pragma: no cover - offline direct-script import path
    from framework import SimulationCommand  # type: ignore


# ---- Error codes (stable string identifiers) -----------------------------

ERR_INVALID_REQUEST = "INVALID_REQUEST"
ERR_INVALID_PARAMS = "INVALID_PARAMS"
ERR_METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
ERR_AXIS_NOT_FOUND = "AXIS_NOT_FOUND"
# 轴处于 BLOCKED 状态,调用方需要先发 reset_collision 。由
# :mod:`modules.axis_errors` 抛出的 :class:`AxisBlockedError` 在 RPC
# handler 里被 catch 后转成这个错误码。保证 RPC 客户端能区分 “axis
# 不存在”、“参数错误”、“axis 被锁” 三类失败,不会全部被压成
# INTERNAL_ERROR。
ERR_AXIS_BLOCKED = "AXIS_BLOCKED"
ERR_INTERNAL = "INTERNAL_ERROR"


class _RpcError(Exception):
    """Raised inside handler bodies to short-circuit with a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- Main-thread bridge --------------------------------------------------


class _MainThreadBridge:
    """Run a callable on Blender's main thread; block the caller until done.

    Only safe to call from worker threads. ``bpy`` data must only be touched
    from the main thread (see ``simulation_manager.py`` module docstring).
    """

    def __init__(self, default_timeout: float = 10.0):
        self._default_timeout = default_timeout

    def call(self, fn: Callable[[], Any], *, timeout: Optional[float] = None) -> Any:
        """Schedule ``fn`` on the main thread via ``bpy.app.timers``.

        Returns ``fn()``'s return value. Re-raises any exception ``fn`` raised
        on the calling thread. Raises ``TimeoutError`` if the main thread
        doesn't run the closure within ``timeout`` seconds.
        """
        import bpy  # lazy: not importable outside Blender

        effective_timeout = (
            self._default_timeout if timeout is None else float(timeout)
        )

        box = {"value": None, "error": None}
        done = threading.Event()

        def posted() -> Optional[float]:
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                box["error"] = exc
            finally:
                done.set()
            return None  # one-shot timer

        bpy.app.timers.register(posted, first_interval=0.0)

        if not done.wait(timeout=effective_timeout):
            raise TimeoutError(
                f"main-thread dispatch timed out after {effective_timeout}s"
            )

        err = box["error"]
        if err is not None:
            raise err
        return box["value"]


# ---- Per-client state ----------------------------------------------------


class _ClientState:
    """Per-connection bookkeeping. ``send_lock`` serialises socket writes so
    the push poller (main thread) and the response writer (client thread)
    cannot interleave bytes.
    """

    __slots__ = (
        "sock",
        "peer",
        "send_lock",
        "buffer",
        "subscribed",
        "interval_s",
        "last_send_ts",
        "close_requested",
    )

    def __init__(self, sock: socket.socket, peer: str):
        self.sock = sock
        self.peer = peer
        self.send_lock = threading.Lock()
        self.buffer = bytearray()
        self.subscribed = False
        self.interval_s = 0.1
        self.last_send_ts: Optional[float] = None
        self.close_requested = False


# ---- Framing helpers -----------------------------------------------------


def _send(state: _ClientState, payload: dict) -> bool:
    """Encode ``payload`` as a newline-terminated JSON message and ``sendall``.

    Returns True on success, False if the socket is broken (OSError). The
    caller should set ``state.close_requested = True`` on False.
    """
    msg = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with state.send_lock:
        try:
            state.sock.sendall(msg)
            return True
        except OSError:
            return False


def _read_message(state: _ClientState) -> Optional[dict]:
    """Read one newline-delimited JSON message.

    Returns the parsed dict, or ``None`` on clean EOF. Partial bytes are
    buffered in ``state.buffer`` between calls. Raises ``_RpcError`` on
    malformed JSON / non-UTF-8 lines (caller should report and continue).
    """
    buf = state.buffer
    while b"\n" not in buf:
        try:
            chunk = state.sock.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    line, _, rest = buf.partition(b"\n")
    state.buffer = bytearray(rest)
    try:
        return json.loads(line.decode("utf-8").strip())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _RpcError(ERR_INVALID_REQUEST, "malformed JSON message")


def _respond(
    state: _ClientState,
    req_id: Any,
    ok: bool,
    *,
    result: Any = None,
    error: Optional[dict] = None,
) -> None:
    payload: Dict[str, Any] = {"ok": ok}
    if req_id is not None:
        payload["id"] = req_id
    if ok:
        payload["result"] = result
    else:
        payload["error"] = error or {"code": ERR_INTERNAL, "message": "unknown error"}
    if not _send(state, payload):
        state.close_requested = True


# ---- Server --------------------------------------------------------------


class RPCServer:
    """JSON-over-TCP RPC server. Default bind 127.0.0.1:9877."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 9877
    DEFAULT_PUSH_INTERVAL_S = 0.1  # 100 ms by default

    def __init__(
        self,
        manager: Any,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        default_push_interval_s: float = DEFAULT_PUSH_INTERVAL_S,
    ):
        self._manager = manager
        self._host = host
        self._requested_port = int(port)
        self._bound_port = 0  # the actual port we ended up bound to
        self._server_sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._clients_lock = threading.Lock()
        self._clients: Dict[int, _ClientState] = {}
        self._next_client_id = 0
        self._bridge = _MainThreadBridge()
        self._push_timer_handle: Any = None  # opaque bpy.app.timers handle
        # 与 SimulationManager 同理：``bpy.app.timers`` 按 **对象身份** 匹配
        # 回调，必须缓存同一个 bound method 对象，否则 is_registered 恒 False、
        # unregister 会抛 "function is not registered"。
        self._push_cb = self._push_tick
        self._default_interval_s = float(default_push_interval_s)

    # ---- public surface ----

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """The actual TCP port the server is bound to. May differ from the
        requested port if the latter was busy (in which case the server
        falls back to an ephemeral port).
        """
        return self._bound_port

    def is_running(self) -> bool:
        return self._server_sock is not None

    def start(self) -> None:
        if self.is_running():
            return

        # Try the requested port; fall back to ephemeral if it's busy so a
        # stale process holding the port doesn't block addon startup.
        try:
            self._server_sock = self._make_listen_sock(self._host, self._requested_port)
            self._bound_port = self._requested_port
        except OSError as exc:
            errno = getattr(exc, "errno", None)
            if errno not in (98, 10048):  # EADDRINUSE on POSIX / WSAEADDRINUSE on Windows
                raise
            print(
                f"[RPC] port {self._requested_port} busy; falling back to ephemeral"
            )
            self._server_sock = self._make_listen_sock(self._host, 0)
            self._bound_port = self._server_sock.getsockname()[1]

        self._stop_event.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="MotionSimulation-RPC-Accept",
            daemon=True,
        )
        self._accept_thread.start()

        self._start_push_timer()

        print(f"[RPC] listening on {self._host}:{self._bound_port}")

    def stop(self) -> None:
        if not self.is_running():
            return

        self._stop_event.set()

        # Closing the listening socket causes accept() to raise; the accept
        # thread will exit naturally and join below.
        try:
            if self._server_sock is not None:
                self._server_sock.close()
        except OSError:
            pass
        self._server_sock = None

        # Close every client socket so their recv loops wake up and exit.
        with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for state in clients:
            try:
                state.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                state.sock.close()
            except OSError:
                pass

        self._stop_push_timer()

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None

        print(f"[RPC] stopped (port {self._bound_port})")
        self._bound_port = 0

    # ---- internal: socket setup ----

    @staticmethod
    def _make_listen_sock(host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(8)
        return sock

    # ---- internal: main-thread push poller ----

    def _start_push_timer(self) -> None:
        """注册推送轮询（已注册则幂等返回）。

        与 :meth:`SimulationManager.start` 同样的坑：Blender 5.1 的
        ``bpy.app.timers.register()`` 返回 ``None``，所以 handle 不能用来判断
        “是否已注册”，这里改用 ``bpy.app.timers.is_registered``。
        """
        import bpy
        if bpy.app.timers.is_registered(self._push_cb):
            return
        self._push_timer_handle = bpy.app.timers.register(
            self._push_cb,
            first_interval=self._default_interval_s,
            persistent=True,
        )

    def _stop_push_timer(self) -> None:
        """注销推送轮询（幂等）。"""
        import bpy
        if not bpy.app.timers.is_registered(self._push_cb):
            self._push_timer_handle = None
            return
        try:
            bpy.app.timers.unregister(self._push_cb)
        except (ValueError, RuntimeError):
            # Already removed (e.g. Blender tearing down).
            pass
        self._push_timer_handle = None

    def _push_tick(self) -> Optional[float]:
        """``bpy.app.timers`` callback. Runs on Blender's main thread."""
        now = time.monotonic()
        try:
            snapshot = (
                self._manager.snapshot() if self._manager is not None else {}
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[RPC] snapshot failed: {exc!r}")
            return self._default_interval_s

        with self._clients_lock:
            clients = list(self._clients.items())

        to_remove: list[int] = []
        for cid, state in clients:
            if state.close_requested or not state.subscribed:
                continue
            last = state.last_send_ts
            if last is not None and (now - last) < state.interval_s:
                continue
            payload = {"type": "state_push", "data": snapshot}
            if not _send(state, payload):
                state.close_requested = True
                to_remove.append(cid)
            else:
                state.last_send_ts = now

        if to_remove:
            with self._clients_lock:
                for cid in to_remove:
                    self._clients.pop(cid, None)
            for cid in to_remove:
                with self._clients_lock:
                    state = self._clients.get(cid)
                # state is None here (we just popped); the client thread
                # won't observe it again.

        return self._default_interval_s

    # ---- internal: accept loop (worker thread) ----

    def _accept_loop(self) -> None:
        sock = self._server_sock
        while not self._stop_event.is_set() and sock is not None:
            try:
                sock.settimeout(0.5)
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                # Listening socket was closed in stop(); exit.
                return

            peer = f"{addr[0]}:{addr[1]}"
            try:
                conn.settimeout(5.0)
            except OSError:
                conn.close()
                continue

            with self._clients_lock:
                cid = self._next_client_id
                self._next_client_id += 1
                state = _ClientState(conn, peer)
                self._clients[cid] = state

            t = threading.Thread(
                target=self._client_loop,
                args=(cid, state),
                name=f"MotionSimulation-RPC-Client-{peer}",
                daemon=True,
            )
            t.start()
            print(f"[RPC] client connected: {peer}")

    # ---- internal: per-client reader (worker thread) ----

    def _client_loop(self, cid: int, state: _ClientState) -> None:
        try:
            while not self._stop_event.is_set() and not state.close_requested:
                try:
                    msg = _read_message(state)
                except _RpcError as exc:
                    if not _respond(
                        state,
                        req_id=None,
                        ok=False,
                        error={"code": exc.code, "message": exc.message},
                    ):
                        state.close_requested = True
                    continue
                if msg is None:
                    return  # clean EOF
                try:
                    self._handle_message(state, msg)
                except _RpcError as exc:
                    _respond(
                        state,
                        req_id=msg.get("id") if isinstance(msg, dict) else None,
                        ok=False,
                        error={"code": exc.code, "message": exc.message},
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[RPC] handler crashed for {state.peer}: {exc!r}")
                    method = msg.get("method") if isinstance(msg, dict) else "?"
                    _respond(
                        state,
                        req_id=msg.get("id") if isinstance(msg, dict) else None,
                        ok=False,
                        error={
                            "code": ERR_INTERNAL,
                            "message": f"{method}: {exc!r}",
                        },
                    )
        finally:
            with self._clients_lock:
                self._clients.pop(cid, None)
            try:
                state.sock.close()
            except OSError:
                pass
            print(f"[RPC] client disconnected: {state.peer}")

    def _handle_message(self, state: _ClientState, msg: Any) -> None:
        """Entry point for a parsed request. Runs on the client thread; any
        access to ``manager`` or ``bpy`` must happen via ``self._bridge``.
        """
        if not isinstance(msg, dict):
            raise _RpcError(ERR_INVALID_REQUEST, "request must be an object")

        req_id = msg.get("id", None)
        method = msg.get("method")
        params = msg.get("params") or {}

        if not isinstance(params, dict):
            raise _RpcError(ERR_INVALID_PARAMS, "params must be an object")
        if not isinstance(method, str) or not method:
            raise _RpcError(
                ERR_INVALID_PARAMS, "method must be a non-empty string"
            )

        if method == "apply_command":
            self._handle_apply_command(state, req_id, params)
        elif method == "subscribe_state":
            self._handle_subscribe(state, req_id, params)
        elif method == "unsubscribe_state":
            self._handle_unsubscribe(state, req_id)
        elif method == "enable_collision_detection":
            self._handle_set_collision_enabled(state, req_id, params, target_enabled=True)
        elif method == "disable_collision_detection":
            self._handle_set_collision_enabled(state, req_id, params, target_enabled=False)
        elif method == "set_outputs":
            self._handle_set_outputs(state, req_id, params)
        elif method == "set_vacuum_enabled":
            self._handle_set_vacuum_enabled(state, req_id, params)
        elif method == "force_release_vacuum":
            self._handle_force_release_vacuum(state, req_id, params)
        elif method == "set_conveyor_running":
            self._handle_set_conveyor_running(state, req_id, params)
        elif method == "ping":
            _respond(state, req_id, ok=True, result={"pong": True})
        else:
            raise _RpcError(
                ERR_METHOD_NOT_FOUND, f"unknown method {method!r}"
            )

    def _handle_apply_command(
        self, state: _ClientState, req_id: Any, params: dict
    ) -> None:
        module_id = params.get("module_id")
        action = params.get("action")
        payload = params.get("payload") or {}
        if not isinstance(module_id, str) or not module_id:
            raise _RpcError(ERR_INVALID_PARAMS, "module_id must be a non-empty string")
        if not isinstance(action, str) or not action:
            raise _RpcError(ERR_INVALID_PARAMS, "action must be a non-empty string")
        if not isinstance(payload, dict):
            raise _RpcError(ERR_INVALID_PARAMS, "payload must be an object")

        def dispatch() -> Any:
            # Validate module exists. ``SimulationManager.apply_command``
            # silently no-ops on missing modules; we want to surface that
            # to the client.
            module = self._manager.get(module_id)
            if module is None:
                raise _RpcError(ERR_AXIS_NOT_FOUND, f"no module {module_id!r}")
            # 把 :class:`modules.axis_errors.AxisBlockedError` 转成专用错误
            # 码,而不是压成 INTERNAL_ERROR —— 客户端应该能区分“轴被锁
            # ”与“其它内部异常”。这里动态 import 避免 rpc 模块在
            # offline 环境下被 bpy-axis_ops 依赖拖崩。
            try:
                from ..modules.axis_errors import AxisBlockedError
            except Exception:  # pragma: no cover - 离线 fallback
                AxisBlockedError = None
            try:
                self._manager.apply_command(
                    module_id, SimulationCommand(action, payload)
                )
            except Exception as exc:
                if AxisBlockedError is not None and isinstance(exc, AxisBlockedError):
                    raise _RpcError(ERR_AXIS_BLOCKED, str(exc)) from exc
                raise
            return None

        result = self._bridge.call(dispatch)
        _respond(state, req_id, ok=True, result=result)

    def _handle_subscribe(
        self, state: _ClientState, req_id: Any, params: dict
    ) -> None:
        interval_ms = params.get("interval_ms", 100)
        try:
            interval_ms = float(interval_ms)
        except (TypeError, ValueError):
            raise _RpcError(ERR_INVALID_PARAMS, "interval_ms must be a number")
        if interval_ms <= 0:
            raise _RpcError(ERR_INVALID_PARAMS, "interval_ms must be > 0")
        state.subscribed = True
        state.interval_s = interval_ms / 1000.0
        state.last_send_ts = None  # send on the next tick
        _respond(
            state,
            req_id,
            ok=True,
            result={"subscribed": True, "interval_ms": interval_ms},
        )

    def _handle_unsubscribe(self, state: _ClientState, req_id: Any) -> None:
        state.subscribed = False
        _respond(
            state, req_id, ok=True, result={"subscribed": False}
        )

    def _handle_set_collision_enabled(
        self, state: _ClientState, req_id: Any, params: dict, *, target_enabled: bool
    ) -> None:
        """Toggle the shared :class:`CollisionEngine`'s enabled flag.

        Routed from two top-level RPC methods:

        - ``enable_collision_detection``  (``target_enabled=True``)
        - ``disable_collision_detection`` (``target_enabled=False``)

        Both are no-arg (``params`` accepted but ignored); this matches
        the symmetry with :meth:`_handle_subscribe` /
        :meth:`_handle_unsubscribe` and removes the ambiguity around
        ``{enabled: true|false}`` (a client always wants the engine
        moved to a specific target state, never "relative" toggling).

        The engine itself is the :attr:`SimulationManager._collision_engine`
        singleton installed by :func:`addon.register` /
        :func:`addon._init_once`. We go through the main-thread bridge
        so the engine flip happens on Blender's main thread (matches
        ``bpy`` discipline for every other RPC handler) — and so
        ``engine.set_disabled``'s side effects (clear the scene flag,
        walk every module and release blocked axes) execute
        consistently with the rest of the simulation.

        ``CollisionEngine.set_disabled`` accepts ``disabled: bool``
        (``True`` = disable, ``False`` = re-enable) and returns
        ``True`` iff the flag actually changed. We invert that to
        match the client contract — the response's ``enabled`` field
        always reflects the engine's post-call state (the one the
        caller asked for, unless the engine was missing).

        Error contract:

        - ``engine is None`` → ``INVALID_PARAMS`` with the same wording
          the dev panel uses (``"Collision engine not initialised"``)
          so the diagnostic message is identical regardless of caller path.
        - ``engine.set_disabled`` raising → ``INTERNAL_ERROR`` (caught
          by the outer ``_client_loop`` handler).
        """
        # params is intentionally not inspected: both methods are
        # no-arg by design. Empty / missing params are accepted as-is.

        def dispatch() -> Any:
            engine = getattr(self._manager, "get_collision_engine", None)
            engine_obj = engine() if callable(engine) else None
            if engine_obj is None:
                raise _RpcError(
                    ERR_INVALID_PARAMS,
                    "Collision engine not initialised",
                )
            # ``set_disabled`` 参数语义：``True`` = 禁用 / ``False`` = 启用。
            # 因此 ``target_enabled=True`` 时传 ``False``（不启用
            # 反而是禁用），与 dev 面板 ``MS_OT_dev_toggle_collision``
            # 的 ``disable_now = engine.is_enabled`` 写法保持一致。
            changed = bool(engine_obj.set_disabled(not bool(target_enabled)))
            return {
                "enabled": bool(target_enabled),
                # ``changed`` 仅在状态翻转时为 True；连续两次发同样指令会
                # 拿到 ``changed=False``，客户端可借此区分“真正切换”与
                # “no-op”。
                "changed": changed,
            }

        result = self._bridge.call(dispatch)
        _respond(state, req_id, ok=True, result=result)

    def _handle_set_outputs(
        self, state: _ClientState, req_id: Any, params: dict
    ) -> None:
        """Set Output_1 / Output_2 control bits on a Cylinder module.

        Routed from the top-level ``set_outputs`` RPC method. Writes
        the two booleans onto the host's CylinderProperty (so the
        panel toggle and the runtime see the same value), and also
        into the runtime's instance mirror so the next tick of the
        :class:`CylinderModule` reflects the new state without
        waiting for the panel mirror path.

        Params
        ------
        - ``module_id`` : Cylinder host name (e.g. ``"Cylinder1"``).
        - ``output_1``  : bool (required).
        - ``output_2``  : bool (required).

        Both outputs must be bool; missing or wrong type ->
        ``INVALID_PARAMS``. Module not found / not a Cylinder ->
        ``AXIS_NOT_FOUND``. Main-thread bridge ensures PropertyGroup
        writes happen on Blender's main thread.
        """
        module_id = params.get("module_id")
        o1 = params.get("output_1")
        o2 = params.get("output_2")
        if not isinstance(module_id, str) or not module_id:
            raise _RpcError(ERR_INVALID_PARAMS, "module_id must be a non-empty string")
        if not isinstance(o1, bool):
            raise _RpcError(ERR_INVALID_PARAMS, "output_1 must be a boolean")
        if not isinstance(o2, bool):
            raise _RpcError(ERR_INVALID_PARAMS, "output_2 must be a boolean")

        def dispatch() -> Any:
            module = self._manager.get(module_id)
            if module is None:
                raise _RpcError(ERR_AXIS_NOT_FOUND, f"no module {module_id!r}")
            if not hasattr(module, "set_outputs"):
                raise _RpcError(
                    ERR_AXIS_NOT_FOUND,
                    f"module {module_id!r} does not support set_outputs",
                )
            module.set_outputs(o1, o2)
            return {"output_1": bool(o1), "output_2": bool(o2)}

        result = self._bridge.call(dispatch)
        _respond(state, req_id, ok=True, result=result)

    def _handle_set_vacuum_enabled(
        self, state: _ClientState, req_id: Any, params: dict
    ) -> None:
        """Set On/Off state on a VacuumNozzle module.

        Routed from the top-level ``set_vacuum_enabled`` RPC method.
        Writes the boolean into the host's VacuumNozzleProperty
        (panel toggle) AND into the runtime's instance mirror
        (so the next tick reconciles without waiting for the
        cfg-mirror path).

        Params
        ------
        - ``module_id`` : VacuumNozzle host name (e.g. ``"VacuumNozzle1"``).
        - ``enabled``   : bool (required).

        Both must be the right type; missing/wrong type ->
        ``INVALID_PARAMS``. Module not found / not a VacuumNozzle ->
        ``AXIS_NOT_FOUND``. Main-thread bridge keeps PropertyGroup
        writes on Blender's main thread.

        Note: ``set_vacuum_enabled`` is intentionally a separate
        top-level method (rather than going through the generic
        ``apply_command`` path) so RPC clients don't need to know
        VacuumNozzle's internal action vocabulary. The mapping is
        symmetric with :meth:`_handle_set_outputs`.
        """
        module_id = params.get("module_id")
        enabled = params.get("enabled")
        if not isinstance(module_id, str) or not module_id:
            raise _RpcError(ERR_INVALID_PARAMS, "module_id must be a non-empty string")
        if not isinstance(enabled, bool):
            raise _RpcError(ERR_INVALID_PARAMS, "enabled must be a boolean")

        def dispatch() -> Any:
            module = self._manager.get(module_id)
            if module is None:
                raise _RpcError(ERR_AXIS_NOT_FOUND, f"no module {module_id!r}")
            if not hasattr(module, "set_enabled"):
                raise _RpcError(
                    ERR_AXIS_NOT_FOUND,
                    f"module {module_id!r} does not support set_enabled",
                )
            module.set_enabled(enabled)
            return {"enabled": bool(enabled)}

        result = self._bridge.call(dispatch)
        _respond(state, req_id, ok=True, result=result)

    def _handle_force_release_vacuum(
        self, state: _ClientState, req_id: Any, params: dict
    ) -> None:
        """Force-release any currently-held object on a VacuumNozzle.

        Routed from the top-level ``force_release_vacuum`` RPC
        method. Bypasses the per-axis BLOCKED gate (escape hatch
        during collision lock); called as a recovery action when
        the rig is in an unexpected state.

        Params
        ------
        - ``module_id`` : VacuumNozzle host name.

        No payload beyond ``module_id``. Returns ``{"released": bool}``
        — True iff something was actually held before the call.
        """
        module_id = params.get("module_id")
        if not isinstance(module_id, str) or not module_id:
            raise _RpcError(ERR_INVALID_PARAMS, "module_id must be a non-empty string")

        def dispatch() -> Any:
            module = self._manager.get(module_id)
            if module is None:
                raise _RpcError(ERR_AXIS_NOT_FOUND, f"no module {module_id!r}")
            if not hasattr(module, "force_release"):
                raise _RpcError(
                    ERR_AXIS_NOT_FOUND,
                    f"module {module_id!r} does not support force_release",
                )
            # 兼容单数(旧 ``_held_obj``)与复数(新 ``_held_objs``)两种槽位。
            held = getattr(module, "_held_objs", None)
            if held is not None:
                was_holding = bool(held)
            else:
                was_holding = getattr(module, "_held_obj", None) is not None
            module.force_release()
            return {"released": was_holding}

        result = self._bridge.call(dispatch)
        _respond(state, req_id, ok=True, result=result)

    def _handle_set_conveyor_running(
        self, state: _ClientState, req_id: Any, params: dict
    ) -> None:
        """Set 启停 / 速度 / 方向 / friction 于一个 Conveyor module。

        Routed from the top-level ``set_conveyor_running`` RPC method.
        与 :meth:`_handle_set_outputs` / :meth:`_handle_set_vacuum_enabled`
        同构——独立的顶层 method,不靠 ``apply_command`` 走 action 词表。

        Params
        ------
        - ``module_id``   : Conveyor host name (e.g. ``"Conveyor1"``)。
        - ``running``     : bool (可选)。是否启停。
        - ``direction``   : int ∈ {-1, +1} (可选)。方向。
        - ``target_speed``: number ≥ 0 (可选)。速度 mm/s。
        - ``friction``    : number ∈ [0.0, 1.0] (可选)。抓地系数。

        所有“可选”参数缺省则不改动;写入 instance + cfg,响应里返回
        本次生效的最终值(反映 cfg 已镜像)。
        模块进 BLOCKED 时仍可改(running=False 解除或改变方向/速度)。
        """
        module_id = params.get("module_id")
        if not isinstance(module_id, str) or not module_id:
            raise _RpcError(ERR_INVALID_PARAMS, "module_id must be a non-empty string")

        # ---- 类型校验(出错即 INVALID_PARAMS)----
        running = params.get("running", None)
        if running is not None and not isinstance(running, bool):
            raise _RpcError(ERR_INVALID_PARAMS, "running must be a boolean")
        direction = params.get("direction", None)
        if direction is not None:
            if isinstance(direction, bool) or not isinstance(direction, int):
                raise _RpcError(ERR_INVALID_PARAMS, "direction must be an int ∈ {-1, +1}")
            if direction not in (-1, 1):
                raise _RpcError(ERR_INVALID_PARAMS, "direction must be -1 or +1")
        target_speed = params.get("target_speed", None)
        if target_speed is not None:
            if isinstance(target_speed, bool) or not isinstance(target_speed, (int, float)):
                raise _RpcError(ERR_INVALID_PARAMS, "target_speed must be a number")
            try:
                target_speed = float(target_speed)
            except (TypeError, ValueError):
                raise _RpcError(ERR_INVALID_PARAMS, "target_speed must be a number")
            if target_speed < 0:
                raise _RpcError(ERR_INVALID_PARAMS, "target_speed must be >= 0")
        friction = params.get("friction", None)
        if friction is not None:
            if isinstance(friction, bool) or not isinstance(friction, (int, float)):
                raise _RpcError(ERR_INVALID_PARAMS, "friction must be a number in [0, 1]")
            try:
                friction = float(friction)
            except (TypeError, ValueError):
                raise _RpcError(ERR_INVALID_PARAMS, "friction must be a number in [0, 1]")
            if friction < 0.0 or friction > 1.0:
                raise _RpcError(ERR_INVALID_PARAMS, "friction must be in [0, 1]")

        def dispatch() -> Any:
            module = self._manager.get(module_id)
            if module is None:
                raise _RpcError(ERR_AXIS_NOT_FOUND, f"no module {module_id!r}")
            kind = getattr(module, "kind", None)
            if kind != "conveyor":
                raise _RpcError(
                    ERR_AXIS_NOT_FOUND,
                    f"module {module_id!r} is a {kind!r}, not 'conveyor'",
                )
            # 顺序:先 set_running(False) 再调其它 setter;setter 互不依赖。
            if running is not None:
                module.set_running(bool(running))
            if target_speed is not None:
                module.set_speed(float(target_speed))
            if direction is not None:
                module.set_direction(int(direction))
            if friction is not None:
                module.set_friction(float(friction))
            snap = {}
            try:
                if hasattr(module, "snapshot"):
                    snap = module.snapshot()
            except Exception:
                snap = {}
            return {
                "running": bool(snap.get("running", running)),
                "direction": int(snap.get("direction", direction if direction is not None else 0)),
                "target_speed": float(snap.get("target_speed", target_speed if target_speed is not None else 0.0)),
                "friction": float(snap.get("friction", friction if friction is not None else 0.0)),
            }

        result = self._bridge.call(dispatch)
        _respond(state, req_id, ok=True, result=result)

# MARKER_PY312_12345