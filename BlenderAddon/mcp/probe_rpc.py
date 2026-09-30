"""Read/write probe for the MotionSimulation RPC server.

Talks JSON-over-TCP to the in-addon RPC server at 127.0.0.1:9877 (started
automatically when the MotionSimulation addon is enabled in Blender).
Verifies ``apply_command`` dispatch against the :class:`SimulationManager`
plus the periodic ``state_push`` subscription.

Usage
-----
Run with the addon enabled in Blender::

    python mcp/probe_rpc.py

Optional args:
    --host H         (default 127.0.0.1)
    --port P         (default 9877)
    --module-id ID   apply_command targets this module_id
                     (default "1", the suffix of LinearAxis1 / RotateAxis1)
    --action ACT     one of home / move_to / velocity / stop / idle
                     (default home)

The probe prints all incoming messages (responses and state pushes) so the
caller can observe the axis state transition during homing.

Exit code is 0 on full success, 1 on any error.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from collections import deque


HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 9877


def _recv_thread(sock: socket.socket, push_queue: "deque[dict]") -> None:
    """Drain the socket on a worker thread; enqueue messages for the main."""
    buf = bytearray()
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return
        if not chunk:
            push_queue.append({"__eof__": True})
            return
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf = bytearray(rest)
            try:
                msg = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
            push_queue.append(msg)


def _send(sock: socket.socket, method: str, params: dict, req_id: str) -> None:
    req = {"id": req_id, "method": method, "params": params}
    sock.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8"))


def _wait_for_id(queue: "deque[dict]", target_id: str, timeout: float):
    """Pop messages until one with ``id == target_id`` shows up, or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if not queue:
            time.sleep(0.05)
            continue
        msg = queue.popleft()
        if msg.get("__eof__"):
            return msg
        if msg.get("id") == target_id:
            return msg
        # State push (no id) — re-enqueue at the front for the caller to see.
        if msg.get("type") == "state_push":
            queue.appendleft(msg)
            time.sleep(0.02)
            continue
        return msg


def main() -> int:
    parser = argparse.ArgumentParser(description="probe MotionSimulation RPC")
    parser.add_argument("--host", default=HOST_DEFAULT)
    parser.add_argument("--port", type=int, default=PORT_DEFAULT)
    parser.add_argument(
        "--module-id",
        default="1",
        help="module_id to target with apply_command (default: '1')",
    )
    parser.add_argument(
        "--action",
        default="home",
        choices=("home", "move_to", "velocity", "stop", "idle"),
    )
    parser.add_argument("--target", type=float, default=None)
    parser.add_argument("--velocity", type=float, default=0.1)
    parser.add_argument("--duration-s", type=float, default=1.0)
    parser.add_argument("--direction", type=int, default=-1)
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=100,
        help="state push interval (default 100ms)",
    )
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=3.0,
        help="seconds to watch state pushes after the apply_command",
    )
    args = parser.parse_args()

    try:
        sock = socket.create_connection((args.host, args.port), timeout=5.0)
    except OSError as exc:
        print(f"error: cannot connect to {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1

    sock.settimeout(2.0)
    queue: deque = deque()
    t = threading.Thread(target=_recv_thread, args=(sock, queue), daemon=True)
    t.start()

    try:
        # 1. ping
        _send(sock, "ping", {}, "ping-1")
        r = _wait_for_id(queue, "ping-1", timeout=5.0)
        if r is None or r.get("__eof__"):
            print(f"error: no response to ping ({r!r})", file=sys.stderr)
            return 1
        print("ping ->", r)
        if not r.get("ok"):
            print(f"error: ping failed: {r!r}", file=sys.stderr)
            return 1

        # 2. subscribe_state
        _send(
            sock,
            "subscribe_state",
            {"interval_ms": args.interval_ms},
            "sub-1",
        )
        r = _wait_for_id(queue, "sub-1", timeout=5.0)
        if r is None or r.get("__eof__"):
            print(f"error: no response to subscribe ({r!r})", file=sys.stderr)
            return 1
        print("subscribe ->", r)
        if not r.get("ok"):
            print(f"error: subscribe failed: {r!r}", file=sys.stderr)
            return 1

        # Give the poller a moment to send at least one push.
        time.sleep(0.5)
        print(
            f"[probe] drained {len(queue)} state_push(es) before apply_command:"
        )
        while queue:
            msg = queue.popleft()
            if msg.get("type") == "state_push":
                print(f"  push: {json.dumps(msg, separators=(',', ':'))[:300]}")
            else:
                queue.appendleft(msg)
                break

        # 3. apply_command
        payload: dict = {}
        if args.action == "home":
            payload = {"direction": args.direction, "velocity": args.velocity}
        elif args.action == "move_to":
            payload = {
                "target_x": args.target if args.target is not None else 0.0,
                "duration_s": args.duration_s,
            }
        elif args.action == "velocity":
            payload = {"velocity": args.velocity}
        # stop / idle carry no payload
        _send(
            sock,
            "apply_command",
            {
                "module_id": args.module_id,
                "action": args.action,
                "payload": payload,
            },
            "apply-1",
        )
        r = _wait_for_id(queue, "apply-1", timeout=5.0)
        if r is None or r.get("__eof__"):
            print(f"error: no response to apply_command ({r!r})", file=sys.stderr)
            return 1
        print(f"apply [{args.action} on {args.module_id}] ->", r)
        if not r.get("ok"):
            print(f"error: apply_command failed: {r!r}", file=sys.stderr)
            # Don't bail — still let the user see any pushes for diagnostics.
        else:
            print(
                f"[probe] watching pushes for {args.watch_seconds}s; the axis should"
                " transition to a target state and (for home) eventually return"
                " to idle with stop_reason='homed'."
            )

        # 4. Drain pushes for a few seconds to show transitions.
        deadline = time.monotonic() + args.watch_seconds
        last_state = None
        while time.monotonic() < deadline:
            if not queue:
                time.sleep(0.05)
                continue
            msg = queue.popleft()
            if msg.get("__eof__"):
                print("[probe] socket closed by server")
                return 1
            if msg.get("type") != "state_push":
                continue
            data = msg.get("data") or {}
            for mid, info in data.items():
                if not isinstance(info, dict):
                    continue
                st = info.get("state")
                if st != last_state:
                    print(
                        f"  {mid}: state={st!r} "
                        f"home_done={info.get('home_done')!r} "
                        f"stop_reason={info.get('stop_reason')!r} "
                        f"pos={info.get('current_x', info.get('current_angle'))}"
                    )
                    last_state = st

        # 5. unsubscribe
        _send(sock, "unsubscribe_state", {}, "unsub-1")
        r = _wait_for_id(queue, "unsub-1", timeout=5.0)
        print("unsubscribe ->", r)

        return 0
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())