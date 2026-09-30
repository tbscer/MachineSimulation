"""Read-only probe: gather rotate-axis state from the user's Blender scene.

Talks JSON-over-TCP to the Blender MCP server at 127.0.0.1:9876 and asks
the running Python interpreter to read the rotate-axis host configuration
plus the world-space orientation of the rotate centre mesh. No scene
modifications.

Usage: ``python probe_rotate.py``
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections import deque


HOST = "127.0.0.1"
PORT = 9876


def main() -> int:
    sock = socket.create_connection((HOST, PORT), timeout=10.0)
    sock.settimeout(0.5)
    buf = bytearray()
    pending: deque = deque()
    lock = threading.Lock()
    cv = threading.Condition(lock)

    def try_parse() -> None:
        nonlocal buf
        s = buf.decode("utf-8", errors="replace").strip()
        if not s:
            return
        while True:
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                return
            consumed = len(s.encode("utf-8", errors="replace"))
            buf = bytearray(buf[consumed:])
            with cv:
                if pending:
                    waiter = pending.popleft()
                    waiter["msg"] = msg
                    waiter["done"] = True
                    cv.notify_all()
                else:
                    print(f"[unexpected push] {msg}", flush=True)
            s = buf.decode("utf-8", errors="replace").strip()
            if not s:
                return

    sock.on_data = None  # placeholder for clarity

    def reader_loop() -> None:
        while True:
            try:
                try_parse()
            except OSError:
                return
            time.sleep(0.05)

    reader_thread = threading.Thread(target=reader_loop, daemon=True)
    reader_thread.start()

    sock.settimeout(0.5)

    def call(type_: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        req = {"type": type_, "params": params or {}}
        with cv:
            waiter = {"done": False, "msg": None}
            pending.append(waiter)
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        deadline = time.monotonic() + timeout
        with cv:
            while not waiter["done"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timeout: {type_}")
                cv.wait(timeout=remaining)
            return waiter["msg"]

    code = """
import bpy
import mathutils

# 1) Confirm host + rotate_axis_index.
hosts = [o for o in bpy.context.scene.objects
         if o.type == 'EMPTY' and o.name.startswith('RotateAxis2')]
print('RotateAxis2 hosts:', [h.name for h in hosts])
for h in hosts:
    cfg = getattr(h, 'rotate_axis', None)
    print(h.name, 'rotate_axis_index =', cfg.rotate_axis_index if cfg else None)

# 2) RotateShaft world orientation.
shaft = bpy.data.objects.get('RotateShaft')
if shaft is None:
    print('NO RotateShaft object')
else:
    print('RotateShaft rotation_mode =', shaft.rotation_mode)
    eul = shaft.rotation_euler
    print('RotateShaft rotation_euler (XYZ order) =',
          (round(eul.x, 6), round(eul.y, 6), round(eul.z, 6)))
    mw = shaft.matrix_world
    print('RotateShaft matrix_world =')
    for row in mw:
        print('  ', tuple(round(v, 6) for v in row))
    # Local +Z in world.
    local_z = (mw.to_3x3() @ mathutils.Vector((0, 0, 1)))
    print('RotateShaft local +Z in world =',
          (round(local_z.x, 4), round(local_z.y, 4), round(local_z.z, 4)))

# 3) Rotator parent.
rotator = bpy.data.objects.get('Rotator')
print('Rotator parent =', rotator.parent.name if rotator and rotator.parent else None)

# 4) World-space AABB of RotateShaft.
if shaft is not None:
    corners = [shaft.matrix_world @ mathutils.Vector(c) for c in shaft.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    print('RotateShaft AABB min =', (round(min(xs),4), round(min(ys),4), round(min(zs),4)))
    print('RotateShaft AABB max =', (round(max(xs),4), round(max(ys),4), round(max(zs),4)))
    print('RotateShaft AABB size =', (round(max(xs)-min(xs),4), round(max(ys)-min(ys),4), round(max(zs)-min(zs),4)))
"""

    try:
        r = call("execute_code", {"code": code})
        print(r["result"]["result"])
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())