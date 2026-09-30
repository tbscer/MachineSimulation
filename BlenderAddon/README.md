# MotionSimulation — V0_3

A Blender Python addon that simulates motion axes. It has evolved from
a single linear-axis simulator into a **unified simulation framework**:
a `SimulationManager` schedules any number of pluggable module
runtimes that all implement the same `BaseSimulationModule` interface.

Currently shipped modules:

| Module | Location | Status |
|---|---|---|
| `LinearAxis` | `modules/LinearAxix/axis.py` | Full runtime (rail + moving body + sensors + optional trigger shim) |
| `RotateAxisRuntime` | `modules/RotateAxis/rotate_axis.py` | Full runtime (rotation centre + Slider + 1 home + up to 2 limit sensors + optional trigger shim) |

The addon is standalone — it does not talk to the `Twin.Mvp` C# WPF
app. It exposes a **local-only** RPC interface on `127.0.0.1:9877`
(see `rpc/`) so that external scripts running on the same machine can
issue axis commands and receive state pushes; there is no LAN binding
and no authentication, so the port must not be exposed beyond
`localhost`.

## Architecture

The architecture mirrors the reference framework at
`Blender_DigitalTwin_LinearAxis_V0_2/`:

| Layer | Module | Role |
|---|---|---|
| Framework | `framework.py` | `BaseSimulationModule` + `SimulationCommand` — the unified module contract |
| Configuration | `modules/LinearAxix/component.py` | `LinearAxisProperty` (`bpy.types.PropertyGroup`) attached to every `bpy.types.Object` via `Object.linear_axis` |
| Simulation loop | `simulation_manager.py` | `SimulationManager` owns the `bpy.app.timers` tick and calls `update(dt)` on every registered module |
| Module factory | `modules/__init__.py` | `build_module(host)` — creates the right runtime for a discovered host |
| Linear axis runtime | `modules/LinearAxix/axis.py` | `LinearAxis` — **passive**. Wraps Slider / Rail / Sensors / Shim; exposes `update(dt)` + command methods |
| Rotate axis runtime | `modules/RotateAxis/rotate_axis.py` | `RotateAxisRuntime` — **passive**. Wraps RotateCenter / Slider / Sensors / Shim; exposes `update(dt)` + `move_to` / `set_velocity` / `home` / `stop` |
| Discovery | `discovery.py` + `modules/LinearAxix/discovery.py` + `modules/RotateAxis/discovery.py` | Unified one-shot entry that delegates each host to its module's own discoverer (`is_host` / `discover` / `clear_flag`) |
| Naming rules | `modules/LinearAxix/naming.py` + `modules/RotateAxis/naming.py` | Per-module naming constants + parsers; single source of truth |
| Components | `modules/components/` | Shared pluggable components: `SensorComponent`, `TriggerShimComponent`, `geometry` |
| Collision | `modules/components/collision.py` + `collision_structure.py` | `CollisionEngine` —— 唯一的碰撞计算入口（集中式拉模式 `step()`）+ 结构声明 / 场景树自动组合 / 组规则 / 结构指纹（纯逻辑） |
| RPC server | `rpc/server.py` | JSON-over-TCP on `127.0.0.1:9877`. Exposes `apply_command(module_id, action, payload)` against `SimulationManager.apply_command`, plus `subscribe_state` for periodic `manager.snapshot()` pushes and `ping` for keepalive |

Every runtime module implements the same interface, so the manager
treats all modules uniformly:

```python
class BaseSimulationModule:
    module_id: str
    kind: str

    def update(self, dt: float) -> None: ...
    def apply_command(self, cmd: SimulationCommand) -> None: ...
    def reset(self) -> None: ...
    def snapshot(self) -> dict: ...

    # 碰撞契约(见 doc/COLLISION_GUIDE.md §0)
    def collision_structure(self) -> CollisionStructure: ...
    def on_collision_hit(self, detail: dict) -> None: ...
    def on_collision_cleared(self) -> None: ...
```

`collision_structure()` 声明本模块的 3D 结构（`members` = host + 部件 +
声明的工件，`bodies` = 运动体）。**组内永不互撞**；CollisionEngine 按**场景树**
取数：先要模块声明，再把未被任何声明认领的根节点**主动包成组合结构**
（`structure:<根名>`，`auto=True`，不漏任何 mesh，`uncovered` 非空就告警）。
两个**模块声明**的组若 host 有祖孙关系（挂在同一载体上）则整对跳过；自动组合
不参与这条豁免。所有重叠计算集中在 `CollisionEngine` 内部，由
`SimulationManager.update()` 在模块循环末尾调一次 `engine.step(depsgraph)`
完成（模块不自己发起碰撞查询）。未实现 `collision_structure()` 的模块自动走
旧字段名单 fallback。

All module runtimes are **passive**: they own no timer, no thread, no
scan. `SimulationManager._tick` is the only place that calls
`update(dt)`.

### Project structure

```
MotionSimulation/
├── __init__.py              # addon entry (register/unregister hooks)
├── addon.py                 # lifecycle orchestrator + manager singleton
├── framework.py             # BaseSimulationModule / SimulationCommand
├── simulation_manager.py    # unified tick scheduler
├── discovery.py             # unified discovery entry + discoverer registry
├── modules/                 # one sub-package per motion module
│   ├── __init__.py          # build_module() factory
│   ├── LinearAxix/          # LinearAxis module
│   │   ├── axis.py          #   runtime
│   │   ├── axis_ops.py      #   command/state helpers
│   │   ├── component.py     #   PropertyGroup configuration
│   │   ├── discovery.py     #   host auto-fill + runtime construction
│   │   ├── naming.py        #   naming conventions
│   │   ├── rail.py          #   RailComponent (travel envelope)
│   │   ├── slider.py        #   SliderComponent (moving body)
│   │   └── ui.py            #   Blender panel / operators
│   ├── RotateAxis/          # RotateAxis module (placeholder)
│   │   └── rotate_axis.py
│   └── components/          # shared pluggable components
│       ├── sensor.py        #   SensorComponent (AABB-overlap detector)
│       ├── trigger_shim.py  #   TriggerShimComponent (clean AABB source)
│       └── geometry.py      #   pure geometry helpers
└── tests/                   # offline pure-Python tests
```

Ownership rationale (per the design docs): everything specific to one
module — runtime, configuration PropertyGroup, naming, command
helpers, discovery, UI — lives inside that module's package; reusable
low-level components live in `modules/components/` so any future
module (e.g. RotateAxis) can share them. Only framework-level pieces
stay at the top level: `framework.py`, `simulation_manager.py`,
`discovery.py` (the entry point) and the addon entry (`__init__.py` /
`addon.py`).

> The reference framework uses `threading.Thread` for the tick. That is
> unsafe — every `bpy` read or write must happen on the main thread.
> This addon uses `bpy.app.timers` instead, which is main-thread.

## Rig setup

```
LinearAxis1 (EMPTY)  ←—— script host
  ├─ Slider  (MESH, child of host)
  │    └─ TriggerShim  (MESH, child of Slider)
  ├─ Rail    (MESH, child of host)
  ├─ Sensor_Origin     (or Sensor_Home)        → home
  ├─ Sensor_Trigger                              → trigger
  ├─ Sensor_NegLimit  (or Sensor_FrontLimit)   → front_limit (negative-X soft stop)
  └─ Sensor_PosLimit  (or Sensor_BackLimit)    → back_limit  (positive-X soft stop)
```

### Naming rules

| Object | Name | Auto-filled PropertyGroup field | Required |
|---|---|---|---|
| Host (EMPTY) | `LinearAxis` + anything (e.g. `LinearAxis1`) | — | yes |
| Slider (MESH) | exactly `Slider` | `slider` | yes |
| Rail (MESH) | exactly `Rail` | `rail` | yes |
| TriggerShim (MESH) | exactly `TriggerShim` (must be child of `Slider`) | `trigger_shim` | no |
| Sensor | `Sensor_Origin` or `Sensor_Home` | `home_sensor` | no |
| Sensor | `Sensor_NegLimit` or `Sensor_FrontLimit` | `neg_sensor` | no |
| Sensor | `Sensor_PosLimit` or `Sensor_BackLimit` | `pos_sensor` | no |

Bare `Sensor` (no suffix) is ignored. Unknown suffixes are ignored
without warning so future sensor kinds can be added without breaking
older scenes.

### PropertyGroup fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `True` | Master toggle. Discovery skips when `False`. |
| `rail` | `Object*` | — | Required. Auto-filled if a child is named `Rail`. |
| `slider` | `Object*` | — | Required. Auto-filled if a child is named `Slider`. |
| `trigger_shim` | `Object*` | — | Optional. Auto-filled from `Slider.children`. |
| `home_sensor` | `Object*` | — | Optional. |
| `pos_sensor` | `Object*` | — | Optional. (`back_limit` kind) |
| `neg_sensor` | `Object*` | — | Optional. (`front_limit` kind) |
| `stroke` | `float` | `1.0` | Total travel distance (m). Currently informational. |
| `speed` | `float` | `0.5` | Default velocity (m/s). |
| `home_speed` | `float` | `0.1` | Homing velocity (m/s). |
| `rotator` | `Object*` | — | Required for a `RotateAxis` host. Auto-filled if a child is named `RotateCenter`. |
| `rotate_home_sensor` | `Object*` | — | Optional. Terminates homing. |
| `rotate_pos_limit_sensor` | `Object*` | — | Optional. Kills positive motion. |
| `rotate_neg_limit_sensor` | `Object*` | — | Optional. Kills negative motion. |
| `rotate_stroke` | `float` | `360.0` | Informational rotation range (deg). |
| `angular_speed` | `float` | `30.0` | Default angular velocity (deg/s). |
| `angular_home_speed` | `float` | `10.0` | Homing angular velocity (deg/s). |
| `rotate_axis_index` | `int` | `2` | `0=X, 1=Y, 2=Z`. Selects which Euler component drives the rotation. |

`PointerProperty(Object)` references are stable: Blender tracks them
by datablock ID, not name. Renaming the referenced object will NOT
break the reference. Renaming the host will break discovery (the
prefix-based filter won't match).

## Rotation-axis rig setup

```
RotateAxis1 (EMPTY)  ←—— script host
  ├─ RotateCenter (MESH, child of host)  ←—— pivot (AABB centre is the rotation axis)
  │    └─ Slider  (MESH, child of RotateCenter; orbits around the centre)
  │         └─ TriggerShim  (MESH, child of Slider)
  ├─ Sensor_Home       (MESH, child of host)        → home (terminates homing)
  ├─ Sensor_PosLimit   (MESH, child of host)        → pos_limit (positive soft stop, optional)
  └─ Sensor_NegLimit   (MESH, child of host)        → neg_limit (negative soft stop, optional)
```

Naming conventions:

| Object | Name | Auto-filled PropertyGroup field | Required |
|---|---|---|---|
| Host (EMPTY) | `RotateAxis` + anything (e.g. `RotateAxis1`) | — | yes |
| RotateCenter (MESH) | exactly `RotateCenter` | `rotator` | yes |
| Slider (MESH) | exactly `Slider` (parented to `RotateCenter`) | `slider` | yes |
| TriggerShim (MESH) | exactly `TriggerShim` (must be child of `Slider`) | `trigger_shim` | no |
| Sensor | `Sensor_Origin` or `Sensor_Home` | `rotate_home_sensor` | no |
| Sensor | `Sensor_PosLimit` | `rotate_pos_limit_sensor` | no |
| Sensor | `Sensor_NegLimit` | `rotate_neg_limit_sensor` | no |

The rotation centre is the source of truth for the current angle:
its `rotation_euler.z` (or whichever axis `rotate_axis_index`
selects) is what the runtime reads and writes. The Slider is just a
child mesh that visually orbits the centre — it never moves locally.

Limit sensors are polarity-gated: `pos_limit` only terminates
positive-direction motion, `neg_limit` only terminates
negative-direction motion. There is no soft rail clamp for rotation
axes; over-travel is treated as a hardware fault, not a normal-mode
condition. If neither sensor is wired, motion follows the command
without a fallback stop.

## State machine

| State | Trigger in | Trigger out |
|---|---|---|
| `idle` | `home()`/`move_to()`/`velocity()`/`stop()` | … |
| `homing` | `home(direction=-1)` | home sensor triggers → `idle` |
| `moving_p2p` | `move_to(x, t)` | elapsed ≥ duration → `idle` |
| `moving_vel` | `set_velocity(v)` | `front_limit`/`back_limit` sensor or rail clamp → `stopped_at_limit` |
| `stopped_at_limit` | (terminal) | next command |

`front_limit` / `back_limit` sensors terminate `moving_vel` early with
`stop_reason="limit"`. The rail clamp inside `SliderComponent` remains
as a hard fallback for scenes without explicit limit sensors.

## Commands (Python API)

```python
from MotionSimulation.modules.LinearAxix.axis import LinearAxis  # not the public path; use axis_ops
from MotionSimulation.modules.LinearAxix.axis_ops import send_command, read_state, refresh_axes

# Send a move command (legacy axis_cmd_* path):
send_command("1", "move_to", target_x=2.0, duration_s=2.0)

# Read state snapshot:
state = read_state("1")
# {"axis_id": "1", "axis_state": "moving_p2p",
#  "axis_current_x": -1.0, "axis_target_x": 2.0, "axis_velocity": 1.5,
#  "axis_moving": True, "axis_sensors": {"Sensor_Origin": False, ...}, ...}

# After editing the scene, force a rebuild:
n = refresh_axes()
```

The Python API on the axis object itself is also available (faster,
no custom-property hop):

```python
from MotionSimulation.addon import get_manager
axis = get_manager().get("1")
axis.home()
axis.set_velocity(0.5)
axis.stop()
```

### Rotate-axis commands

Same vocabulary, different units. Angles are degrees, velocity is
deg/sec. The shortest-arc path is chosen for `move_to` (so `move_to(270)`
from `0°` rotates `-90°`, not `+270°`).

```python
from MotionSimulation.modules.RotateAxis.axis_ops import (
    send_command, read_state, refresh_axes,
)

send_command("1", "move_to", target_angle=270.0, duration_s=2.0)
send_command("1", "velocity", velocity=45.0)   # 45 deg/s
send_command("1", "home", home_direction=-1, velocity=10.0)

state = read_state("1")
# {"axis_id": "1", "axis_state": "moving_p2p",
#  "axis_current_angle": 90.0, "axis_target_angle": 270.0,
#  "axis_velocity": -90.0, "axis_moving": True, ...}

axis = get_manager().get("1")
axis.move_to(90.0, duration_s=1.5)
axis.set_velocity(30.0)
axis.home()
axis.stop()
```

## State outputs (`axis_*` custom properties on the Slider)

These are written by the simulator every tick. Read-side only —
external callers should not write them. They're the public read
surface for MCP `execute_code`.

| Property | Type | Notes |
|---|---|---|
| `axis_state` | `string` | One of `idle` / `homing` / `moving_p2p` / `moving_vel` / `stopped_at_limit` |
| `axis_current_x` | `float` | Slider X right now (LinearAxis only) |
| `axis_current_angle` | `float` | Centre rotation in degrees, wrapped to (-180, 180] (RotateAxis only) |
| `axis_has_target` | `bool` | True during `moving_p2p` |
| `axis_target_x` | `float` | Active P2P target (LinearAxis) |
| `axis_target_angle` | `float` | Active P2P target in degrees (RotateAxis) |
| `axis_velocity` | `float` | Current signed velocity (m/s linear, deg/s rotation) |
| `axis_moving` | `bool` | True in any motion state |
| `axis_home_done` | `bool` | Has the axis ever homed |
| `axis_home_x` | `float` | Captured X on homing (LinearAxis) |
| `axis_home_angle` | `float` | Captured angle on homing (RotateAxis) |
| `axis_stop_reason` | `string` | `arrived` / `homed` / `limit` / `cmd_idle` / `cmd_stop` |
| `axis_sensors` | `string` | JSON dict `{sensor_name: is_triggered}` |

`is_triggered` is also written to each sensor as a custom property
for compatibility with `Twin.Mvp/Twin.Blender/twin_runtime_addon.py`.

## Command inputs (`axis_cmd_*` custom properties on the Slider)

Written by `axis_ops.send_command`. Read by `LinearAxis._consume_cmd`
or `RotateAxisRuntime._consume_cmd` on every tick (strict monotonic
`axis_cmd_seq` is the trigger).

| Property | Type | Notes |
|---|---|---|
| `axis_cmd_seq` | `int` | Monotonic counter; consume only on strictly increasing value |
| `axis_cmd_action` | `string` | One of `idle` / `stop` / `home` / `move_to` / `velocity` / `reset_collision` |
| `axis_cmd_target_x` | `float` | P2P target (LinearAxis) |
| `axis_cmd_target_angle` | `float` | P2P target angle in degrees (RotateAxis) |
| `axis_cmd_duration_s` | `float` | P2P duration |
| `axis_cmd_velocity` | `float` | Signed velocity (m/s linear, deg/s rotation) |
| `axis_home_direction` | `int` | `-1` (default) or `+1` |

## Discovery behaviour

- Runs **once** at addon enable (deferred past Blender's
  `_RestrictData` window via a zero-delay `bpy.app.timers` callback).
- The top-level `discovery.py` walks `scene.objects` and delegates each
  object to the first registered discoverer whose `is_host` matches.
  The LinearAxis discoverer (`modules/LinearAxix/discovery.py`) claims
  `LinearAxis*` EMPTY hosts; the RotateAxis discoverer
  (`modules/RotateAxis/discovery.py`) claims `RotateAxis*` EMPTY hosts.
  Each module registers its own discoverer with the unified registry.
- For each host: the discoverer auto-fills empty
  `PointerProperty(Object)` fields from the naming convention, builds
  the runtime module, and the entry registers it with
  `SimulationManager`.
- Marks the host with a discovery flag (e.g.
  `_linear_axis_discovered=True` / `_rotate_axis_discovered=True`) to
  make the scan idempotent.
- There is **no per-tick rebuild**. Edit the scene then call
  `refresh_axes()` (from either module's `axis_ops`) or disable+re-enable
  the addon.

## Migration from V0_1

V0_1 used `linear_axis_role` / `linear_axis_id` / `rail_x_min` /
`default_velocity` / `home_velocity` / `linear_axis_sensor_kind`
custom properties on every participating object. V0_3 ignores all of
these. The live scene in this repo already follows the V0_3 naming
convention (`LinearAxis1` host + `Slider` / `Rail` / `Sensor_Origin` /
`Sensor_NegLimit` / `Sensor_PosLimit` children), so enabling V0_3
should "just work" — no scene edits required.

For scenes that still use V0_1 names (`Motion_Body` / `Rail_Front` /
`Sensor_Trigger` / `Sensor_FrontLimit` / `Sensor_BackLimit`):

1. Rename `Motion_Body` → `Slider`.
2. Rename `Rail_Front` → `Rail`.
3. Rename sensors to match the table above (or add aliases:
   `Sensor_FrontLimit` is already an alias for `front_limit`).
4. Optionally wrap everything under an EMPTY named `LinearAxis_1` (or
   `LinearAxis1`).

## Rail direction (long-edge motion vector)

The `Rail` mesh is the source of truth for the linear-axis motion
direction. The runtime no longer assumes world-X:

- `RailComponent` computes the rail's world-space AABB at construction
  time and picks the **longest** edge as the motion direction (a 3-D
  unit vector). Ties prefer X, then Y, then Z, so legacy axis-aligned
  rigs behave exactly as before.
- The slider's internal `current_x` is a **scalar** measured along
  this direction with the rail's AABB centre as the origin (t = 0).
- On every position update the slider writes its 3-D world location as
  `rail.center + rail.direction * current_x`. The Y and Z location
  channels of the slider are no longer locked at construction so the
  slider can follow a non-X rail correctly.
- The rail envelope (`x_min` / `x_max`) is the scalar range of the
  rail's AABB projected onto the direction, so `clamp(t)` works
  uniformly regardless of the rig's rotation.

This means an artist can place a `Slider` and `Rail` at any
orientation in the scene (e.g. a vertical Y-axis slide, a 30°
diagonal) and the runtime does the right thing. The MCP/RPC API
(`target_x` as a scalar distance) is unchanged.

Degenerate rails (zero-extent AABB) fall back to world X. When the
slider is constructed without a rail (the offline test harness path)
it falls back to writing `obj.location.x` only.

## Collision detection (BVH-based)

The runtime supports physics-engine-based collision detection between
axis moving bodies and arbitrary scene obstacles. Implementation is in
`modules/components/collision.py`; integration is per-module
(`LinearAxis.update` / `RotateAxisRuntime.update` /
`CylinderModule.update`), all sharing one `CollisionEngine` singleton.

### How it works

- The shared `CollisionEngine` (constructed once in `addon.py`)
  maintains a BVH cache of every non-axis mesh in the scene. The BVH
  is rebuilt every `rebuild_every_n_ticks` (default 30, ≈ once per
  second at 30 Hz) and on demand via `force_rebuild()` after
  discovery. The counter is **per axis**
  (`rebuild_if_due(depsgraph, axis_id=…)`) and every real rebuild
  resets all counters, so the cadence is N ticks regardless of how
  many axes share the engine.
- Per tick, each module builds a BVH for its own moving body
  (slider / rotator / cylinder `work_bar`) and queries
  `BVHTree.overlap` against the per-axis BVH cache and the obstacle
  cache. The check uses Blender's own `evaluated_depsgraph_get()` so
  modifiers and shape keys are reflected — even while collision
  detection is disabled, so the Dev panel can keep showing what
  *would* be hit. Locking an axis into `STATE_BLOCKED` is gated by
  `engine.is_enabled` separately.
- BVH overlap (triangle vs triangle) replaces the legacy AABB math:
  complex concave obstacles are detected precisely without the
  false-positives of bounding-box overlap.

### Scene marker

A single boolean custom property on the scene is the source of truth:

| Key | Type | Set by | Read by |
|---|---|---|---|
| `scene["motion_simulation_collision"]` | `bool` | `CollisionEngine.mark_collision()` on the first overlap | Every module on its next tick |

When **any** module detects a collision the engine writes `True` to
this flag. On the next tick every module (axes and cylinders) sees the
flag and transitions to `STATE_BLOCKED` with `stop_reason="collision"`.
The flag stays `True` until cleared so an external controller can read
it at any time.

### Blocked state and `reset_collision`

- New state: `STATE_BLOCKED = "blocked"` (added to `ALL_STATES` for
  every module kind).
- New stop-reason: `"collision"`.
- New action: `reset_collision` (added to `VALID_ACTIONS` for every
  module kind).
- While in `STATE_BLOCKED`, `LinearAxis` / `RotateAxisRuntime` refuse
  every command except `reset_collision`; other commands (`move_to`,
  `velocity`, `home`, `stop`, `idle`) are silently consumed so the
  same `axis_cmd_seq` is not reprocessed. `CylinderModule` (driven by
  `apply_command` instead of a custom-property queue) rejects the same
  set, but additionally still accepts `set_outputs`, since those two
  bits are an external control signal rather than a motion command.
- `reset_collision` is the **only** way to clear the flag. The
  command is accepted by any module; the receiving module calls
  `engine.clear_collision()` and returns itself to `STATE_IDLE`. On
  the next tick every other blocked module sees the cleared flag and
  auto-unblocks without needing its own command.
- Disabling collision detection altogether
  (`CollisionEngine.set_disabled(True)`, the Dev panel's
  **[Disable]** button, RPC `disable_collision_detection`) is a real
  kill switch: `mark_collision` becomes a no-op, `read_marker` always
  returns `False`, and every blocked module is released immediately
  through `_clear_blocked_state_only()`.

Send the reset via the same pipeline as other commands:

```python
# MCP execute_code
import bpy
bpy.data.objects["LinearAxis1.Slider"]["axis_cmd_seq"] = N + 1
bpy.data.objects["LinearAxis1.Slider"]["axis_cmd_action"] = "reset_collision"

# Or via the Python API
axis = manager.get_module("LinearAxis1")
axis.reset_collision()
```

### Obstacle detection rules

- Auto-detected: every `MESH` in the scene that is **not** part of a
  registered module (host, slider, rail, rotator, center, shim,
  work_bar, touch_shim, approach sensors — see
  `SimulationManager._AXIS_OBJECT_FIELDS`).
- Excluded: empty / non-mesh types, meshes with no polygons,
  `hide_viewport` / `hide_render`.
- Blender's own per-object `obj.collision.use` flag is **deliberately
  ignored** — that toggle belongs to Blender's rigid-body / cloth
  solver, and every mesh the artist places in the scene is a valid
  obstacle for this addon.
- Other **axes'** bodies are valid obstacles: cross-axis collisions
  are real hits (each axis is a single combined BVH, and only the
  slider's own axis is skipped). Exceptions: parent/child axis pairs
  (`_axes_are_related`, e.g. a Y axis mounted on an X carrier) are
  treated as one payload and never cross-collide.

### Performance

At 30 Hz with 10 axes and 50 obstacles (~500 triangles each):

- Obstacle BVH rebuild: ~5–10 ms amortised per tick (default 30-tick
  cadence).
- Per-slider BVH build + overlap queries: ~2–5 ms per tick.
- Total: well within the 33 ms tick budget.

Tune `rebuild_every_n_ticks` on the engine (or call `force_rebuild`
explicitly) for larger scenes.
5. Strip all old custom properties from the rig objects.

## Running the tests

```bash
cd <addon-root>/MotionSimulation
python tests/test_geometry.py                     # 16 tests
python tests/test_slider.py                       # 18 tests
python tests/test_naming.py                       # 14 tests (LinearAxis + RotateAxis naming)
python tests/test_discovery.py                    # 20 tests (LinearAxis + RotateAxis discovery)
python tests/test_simulation_manager.py           # 20 tests (registry + owned-name collection)
python tests/test_framework.py                    # 7 tests (module interface + manager)
python tests/test_rotate_axis.py                  # 67 tests (rotate-axis end-to-end)
python tests/test_cylinder.py                     # 31 tests (cylinder runtime + collision contract)
python tests/test_collision.py                    # 43 tests (BVH engine + pure helpers + cadence)
python tests/test_axis_fence.py                   # 9 tests (owned-name fence / ancestry)
python tests/test_axis_blocked_state.py           # 25 tests (BLOCKED contract: linear + rotate)
python tests/test_collision_disable.py            # 12 tests (enable / disable toggle)
python tests/test_collision_state_snapshot.py     # 11 tests (snapshot "collision" block)
python tests/test_rpc_collision_enable_disable.py # 11 tests (RPC enable / disable)
python tests/test_approach_sensor.py              # 18 tests
python tests/test_u_sensor.py                     # 13 tests
python tests/test_home_backoff.py                 # 8 tests
python tests/test_rail_direction.py               # 14 tests
python tests/test_jog_and_sensor_snapshot.py      # 47 tests
python tests/test_timer_lifecycle.py              # 14 tests
python tests/test_rpc_set_outputs.py              # 7 tests
```

Or via pytest:

```bash
pytest tests/
```

> 当前工作副本里还有 13 个与 `slider` / `rail` / `RotateAxis` 接线有关的
> 历史失败用例（与本 collision 改动无关），跑全量时它们是红的。

The tests are pure-Python and run without Blender. `bpy.app.timers`
is exercised through a guarded import (`SimulationManager.stop`
only imports `bpy` when a timer was actually registered), so the
offline tests don't require Blender.

## Decoupling

`Twin.Mvp/` and this addon share zero code and zero runtime state.
The only thing they share in the same `.blend` is the `is_triggered`
custom property name on sensors (so both can read each other's
sensor edges). To verify decoupling:

```bash
grep -r "Twin\|websocket\|WebSocket\|9877" \
    <addon-root>/MotionSimulation
# expected: no matches
```

## 安装
● 在 Blender 里分两种用法,我都给你。

  A. 作为正式插件安装(推荐,长期用)

  1. 启用插件
    - 菜单 Edit → Preferences → Add-ons
    - 右上角 Install...(Blender 4.0+ 是 Get Extensions 旁的下拉 → Install from Disk)
    - 选整个文件夹:MotionSimulation(addon 根目录,内含 blender_manifest.toml)
    - 搜索 "LinearAxis" → 勾选启用
    - 左下角 ⚙️ → Save Preferences(下次开 Blender 自动加载)
  2. 搭场景(你的场景已经满足,跳过即可)
    - 建一个 Empty: Add → Empty → Plain Axes,改名 LinearAxis1
    - 把 Slider、Rail、各 Sensor_* 选中 → Ctrl+P → Object 挂到 LinearAxis1 下
    - Slider.TriggerShim 用同样方式挂到 Slider 下
    - 命名必须严格:子物体叫 Slider/Rail,sensor 叫 Sensor_Origin/Sensor_NegLimit/Sensor_PosLimit 等
  3. 看配置:选中 LinearAxis1 → 右侧 Properties 面板 → Object 属性(橙色方块图标) → 底部会多出一个 Linear Axis 分组,里面是
  enabled/slider/rail/speed/home_speed 等字段。留空的话插件会按命名自动填充。
  4. 驱动:目前没做 UI 按钮,驱动要靠脚本(见 B 的 Text Editor)或 MCP。

  B. 临时加载 + Text Editor 驱动(测试/调试)

  1. 顶部切到 Scripting 工作区(或 Text Editor 窗口)
  2. New 一个新文本块,粘贴:

  import sys
  sys.path.insert(0, r"<addon 父目录,如 .../scripts/addons>")

  # 首次加载(改过代码后先跑这段重载)
  for k in list(sys.modules):
      if k.startswith("MotionSimulation"):
          del sys.modules[k]
  import MotionSimulation
  MotionSimulation.register()
  print("已发现轴:", [a.axis_id for a in MotionSimulation.addon.get_manager().all()])

  # ---- 驱动运动 ----
  from MotionSimulation.addon import get_manager
  ax = get_manager().get("1")
  ax.home()                        # 回零
  # ax.move_to(2.0, duration_s=2.0)  # 点到点
  # ax.set_velocity(0.5)             # 定速
  # ax.stop()

  # ---- 读状态 ----
  print("state:", ax.state, "x:", round(ax.slider.current_x, 3))
  3. Alt+P 运行。每次改完代码,先跑重载那段再跑驱动。

  注意:V0_3 是一次性发现。如果在运行时改了场景结构,跑 from MotionSimulation.modules.LinearAxix.axis_ops import refresh_axes; refresh_axes() 重新发现,或重新执行
  MotionSimulation.register()。
  
  ## 安装2
  正确的安装步骤(关键:要打包成 zip)

  现代 Blender 的 manifest 插件必须通过 zip 安装,不能直接指向文件夹。

  1. 打包:把 MotionSimulation 文件夹里面的内容压缩成 zip,保证 blender_manifest.toml 在 zip 的根目录(不要多套一层文件夹)。
    - 在 MotionSimulation 目录里全选所有文件 → 右键 Compress to ZIP
    - 验证:打开 zip,第一眼应看到 blender_manifest.toml、__init__.py、addon.py… 而不是一个 MotionSimulation/ 文件夹
  2. 安装:Edit → Preferences → Add-ons → Install...(Blender 5.1 是右上角下拉 Install from Disk)→ 选那个 zip
  3. 启用:搜 "LinearAxis" → 勾选 → 左下角 Save Preferences
  4. 看报错:如果还失败,先 Window → Toggle System Console 打开控制台,再启用,报错会显示在那个黑窗口里。把报错贴给我。

  如果 zip 安装还是失败,用脚本注册(绕过 UI,一定能看到错误)

  在 Scripting 工作区新建文本块,Alt+P 运行:
  ``` python
  import bpy, sys, traceback
  # 把 addon 父目录注册为可导入路径,并用脚本目录方式加载
  path = r"<addon 父目录,如 .../scripts/addons>"
  if path not in sys.path:
      sys.path.insert(0, path)
  try:
      import MotionSimulation
      MotionSimulation.register()
      print("✅ 注册成功,发现轴:", [a.axis_id for a in MotionSimulation.addon.get_manager().all()])
  except Exception:
      print("❌ 注册失败:")
      traceback.print_exc()   # 完整报错会打在这里
  ```
  这段在我这边已经验证过能成功(发现 axis_id=1)。如果你跑这段也报错,把控制台输出贴给我,我就能精确定位。