# MotionSimulation RPC 通讯协议

> 本文档面向 **外部 RPC 客户端开发者**（C# / Python / 任何能开 TCP socket 的进程），
> 说明如何通过 `127.0.0.1:9877` 与 Blender 内的 MotionSimulation addon 对话。
>
> **版本对应**：本协议随仓库当前 master 实现；如未来 RPC 字段调整，请同步更新本文档。

**导读（按要解决的问题选章）**：

| 我想…… | 去哪章 |
|---|---|
| 连上 server / 搞清帧格式 | §1 端口与传输、§2 帧格式 |
| 看一条命令从 socket 到模块的完整链路 | §3 完整调用链路 |
| 查某个 method 的参数 / 响应 / 错误 | §4 RPC method 详细定义 |
| 搞懂 state_push 推流、顶层 bucket | §5 state_push 数据格式 |
| **按模块查功能**（某模块能干什么、怎么控、BLOCKED 行为） | **§6 按模块功能速查** |
| 开关全局碰撞 / 解除 BLOCKED | §4.4、§4.5 + §5.1 + §6.0 |
| 查错误码 | §7 错误码 |
| 抄客户端代码 | §8 客户端参考实现 |
| 避坑 | §9 已知坑 / 易错点 |
| 找源码 | §10 相关源码位置 |

---

## 1. 端口与传输

| 项目 | 值 |
|------|-----|
| 监听地址 | `127.0.0.1`（loopback only，**不要暴露到外网**） |
| 监听端口 | `9877`（可在 `RPCServer` 构造时改） |
| 传输层 | TCP，**JSON over newline-delimited lines**（每条消息以 `\n` 结尾） |
| 编码 | UTF-8 |
| 帧格式 | 一行一个完整 JSON 对象 |
| 鉴权 | 无（loopback 假定安全） |
| 端口冲突 | 占用时自动 fallback 到 ephemeral port；可通过 `RPCServer.port` 拿到实际端口 |

> **不要与 blender-mcp（端口 9876）混淆**：9876 是 ahujasid/blender-mcp addon，
> 用 `{"type": ..., "params": ...}` 协议；9877 是本项目自己的 RPC server，
> 用 `{"id": ..., "method": ..., "params": ...}` 协议。两条链路独立，可同时存在。

---

## 2. 帧格式

### 2.1 请求
```json
{
  "id": <任意可 JSON 序列化的标识符，类型不限>,
  "method": "<method 名>",
  "params": { ... 方法专属参数 ... }
}
```

### 2.2 响应（同步回包）
```json
{
  "ok": true | false,
  "id": <与请求 id 一致>,
  "result": <方法返回> | null,
  "error": {"code": "<ERR_*>", "message": "..."}     // 仅 ok=false 时
}
```

### 2.3 推流（state_push）
由订阅触发的 server-pushed 消息，**没有 id 字段**，不能与请求关联：
```json
{
  "type": "state_push",
  "data": { "<category>": [ { ...snapshot... }, ... ], ... }
}
```

---

## 3. 完整调用链路

```
RPC Client (TCP 127.0.0.1:9877, JSON over newline-delimited lines)
  │
  │  1 行 JSON: {"id": N, "method": "...", "params": {...}}
  ▼
RPCServer._client_loop  (per-connection worker thread)
  ├─ _read_message(state)                  ← rpc/server.py
  │   ├─ 从 state.buffer 累积字节
  │   ├─ 拆到第一个 '\n'
  │   └─ json.loads(line) → dict
  │
  ├─ _handle_message(state, msg)
  │   ├─ 校验 id / method / params 类型
  │   ├─ 路由:
  │   │   ├─ "apply_command"            → _handle_apply_command
  │   │   ├─ "subscribe_state"          → _handle_subscribe
  │   │   ├─ "unsubscribe_state"        → _handle_unsubscribe
  │   │   ├─ "enable_collision_detection"   → _handle_set_collision_enabled(target_enabled=True)
  │   │   ├─ "disable_collision_detection"  → _handle_set_collision_enabled(target_enabled=False)
  │   │   ├─ "set_outputs"              → _handle_set_outputs            (Cylinder)
  │   │   ├─ "set_vacuum_enabled"       → _handle_set_vacuum_enabled     (VacuumNozzle)
  │   │   ├─ "force_release_vacuum"     → _handle_force_release_vacuum   (VacuumNozzle)
  │   │   ├─ "set_conveyor_running"     → _handle_set_conveyor_running   (Conveyor)
  │   │   ├─ "ping"                     → _respond({"pong": true})
  │   │   └─ 其他                  → _RpcError(METHOD_NOT_FOUND)
  │   └─ _RpcError 被外层捕获 → _respond(ok=False, error={code, message})
  │
  └─ _handle_apply_command(state, req_id, params)
        ├─ 校验 module_id / action / payload 类型
        ├─ 构建 dispatch() 闭包
        ├─ self._bridge.call(dispatch)               ← rpc/server.py
        │   ├─ 用 bpy.app.timers.register(posted, first_interval=0.0)
        │   │   把闭包 marshal 到 Blender 主线程（bpy 数据只能主线程碰）
        │   └─ 主线程上跑 posted()：
        │       ├─ module = self._manager.get(module_id)
        │       │   └─ None → raise _RpcError(AXIS_NOT_FOUND)
        │       ├─ 轴类 BLOCKED → raise AxisBlockedError → RPC 错误 AXIS_BLOCKED
        │       └─ self._manager.apply_command(
        │              module_id,
        │              SimulationCommand(action, payload))    ← framework.py
        │           │
        │           ▼  SimulationManager.apply_command
        │       module = self.get_module(module_id)
        │       if module is None: return                  ← 注意：manager 这层吞 unknown
        │       module.apply_command(cmd)
        │           │
        │           ▼  module-specific dispatch
        │       ┌────────────────────────┬───────────────────────────┐
        │       │ LinearAxis             │ RotateAxisRuntime         │
        │       │ apply_command          │ apply_command             │
        │       │ axis.py                │ rotate_axis.py            │
        │       └────────────────────────┴───────────────────────────┘
        │           │ (按 cmd.action 分发; 各模块自带 BLOCKED 门控)
        │           ├─ "move_to"          → move_to(target, velocity / duration_s)
        │           ├─ "jog"             → set_velocity(direction * |velocity|)
        │           ├─ "home"            → home(direction, velocity)
        │           ├─ "velocity"        → set_velocity(v) / set_velocity(omega)
        │           ├─ "stop"/"idle"     → stop()/idle()
        │           └─ 其他 action        → 静默忽略 (ok=true, 见 §9 坑 10)
        │           │
        │           ▼  slider / rotator 调用
        │       slider.start_move_to / start_homing / start_velocity / start_idle
        │           ↓ 写到 self._velocity / self._home_command_velocity
        │
        └─ _respond(state, req_id, ok=True, result=null)   ← 一行 JSON 回包

┌──────────────────────────────────────────────────────────────────────────┐
│ 并行通道: 状态推流                                                        │
│ RPCServer._push_tick (bpy.app.timers, 默认 100ms, 主线程)               │
│   ├─ snapshot = self._manager.snapshot()                                  │
│   │     └─ {category: [module.snapshot(), ...]}  ← 按 category 分桶,见 §5 │
│   ├─ 对每个 subscribed 的 client:                                         │
│   │     _send(state, {"type": "state_push", "data": snapshot})            │
│   └─ 没有任何 id 字段，客户端不能用 id 关联                              │
└──────────────────────────────────────────────────────────────────────────┘
```

**关键不变量**：
- bpy 数据只能在主线程碰 → 所有模块调用通过 `_MainThreadBridge.call` 异步调度
- socket 写由 per-client `send_lock`（`threading.Lock`）序列化，推流（主线程）与响应（client thread）不会交错字节
- `SimulationManager.apply_command` 静默 no-op on unknown module；RPC 层在 dispatch 闭包里**先** `manager.get(module_id)` 显式检查，所以 RPC 客户端拿到的是 `AXIS_NOT_FOUND`

---

## 4. RPC method 详细定义

**method 总表**（10 个）：

| method | 归属 | 说明 |
|---|---|---|
| `ping` | 通用 | 健康检查 |
| `subscribe_state` / `unsubscribe_state` | 通用 | 打开 / 关闭 state_push 推流 |
| `apply_command` | 通用 | 向任意模块派发 action（词表见 §4.6 与 §6 各模块） |
| `enable_collision_detection` / `disable_collision_detection` | 全局 | 碰撞引擎开关（兼解除 BLOCKED，见 §6.0） |
| `set_outputs` | Cylinder | 输出位 |
| `set_vacuum_enabled` / `force_release_vacuum` | VacuumNozzle | 真空开关 / 强制释放 |
| `set_conveyor_running` | Conveyor | 启停 / 调速 / 方向 / 摩擦 |

### 4.1 `ping`

健康检查 / keepalive。

**请求**
```json
{"id": 1, "method": "ping", "params": {}}
```

**响应**
```json
{"ok": true, "id": 1, "result": {"pong": true}}
```

无副作用，无错误。

---

### 4.2 `subscribe_state`

打开本连接的 state_push 推流。

**请求**
```json
{"id": 2, "method": "subscribe_state", "params": {"interval_ms": 100}}
```

**参数**
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `interval_ms` | number | 否 | 推流间隔（毫秒），必须 `> 0`，默认 `100` |

**响应**
```json
{"ok": true, "id": 2, "result": {"subscribed": true, "interval_ms": 100.0}}
```

**副作用**
- `state.subscribed = True`，下一个 push tick 立即推一次
- 重复调用只更新间隔（幂等）

**错误**
- `INVALID_PARAMS` — `interval_ms` 非数字或 `<= 0`

---

### 4.3 `unsubscribe_state`

关闭本连接的 state_push 推流。

**请求**
```json
{"id": 3, "method": "unsubscribe_state", "params": {}}
```

**响应**
```json
{"ok": true, "id": 3, "result": {"subscribed": false}}
```

幂等，重复调用 OK。

---

### 4.4 `enable_collision_detection`

启用全局共享的 :class:`CollisionEngine`（`engine.set_disabled(False)`），
让每个 tick 的模块运行时恢复“命中即阻塞 + 写 scene flag”。

无参数；调用后引擎处于 ENABLED 状态。**幂等**：连续两次调用是 no-op，
但响应里的 `changed` 字段会反映状态是否真的翻转。

注意 `set_disabled` **无论开还是关都会先 `clear_collision()`** ——
重新启用时顺手清掉可能残留的 scene flag，避免“一开就全体 blocked”。
这也是 **通过 RPC 解除轴类（LinearAxis / RotateAxis）BLOCKED 的标准姿势**
（轴类 apply_command 不收 `reset_collision`，见 §6.1 / §6.2 / §6.0）。

> 引擎被禁用时（`disable_collision_detection`）：
> - `read_marker()` 恒为 `False`，所以被锁的模块不会再被 marker 钉住；
> - 命中时运行时还会检查 `engine.is_enabled`，**不会**重新进入 `STATE_BLOCKED`；
> - `check_slider_collision` 仍然每 tick 跑完 BVH 检测并更新
>   `last_collision`，供 Dev panel 显示“会撞到什么”。
>
> 这是“artist-facing kill switch”，用于“把 rig 停在障碍里检查”的场景。

**请求**
```json
{"id": 5, "method": "enable_collision_detection", "params": {}}
```

**响应**
```json
{"ok": true, "id": 5, "result": {"enabled": true, "changed": true}}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | bool | 目标状态（永远为 `true`） |
| `changed` | bool | 引擎状态是否真的翻转了（已 enabled 时调用为 `false`） |

**错误**
- `INVALID_PARAMS` — 引擎未构造（addon 还没 `set_collision_engine`）。
  错误文案与 Dev 面板 `MS_OT_dev_toggle_collision` 一致：
  `"Collision engine not initialised"`
- `INTERNAL_ERROR` — `engine.set_disabled` 抛异常（理论上不会发生）

---

### 4.5 `disable_collision_detection`

停用全局共享的 :class:`CollisionEngine`（`engine.set_disabled(True)`）：
scene flag 被清为 `False`，并且所有处于 `STATE_BLOCKED` 的模块
（LinearAxis / RotateAxisRuntime / CylinderModule / ConveyorModule /
VacuumNozzle / ApproachSensor）**立即**被 `_clear_blocked_state_only()` 释放
—— 不等下一 tick。

“跳过碰撞检测”只指“不再锁进 BLOCKED”：BVH 检测本身仍在跑，
`last_collision` 仍在更新（见 §4.4）。

无参数；调用后引擎处于 DISABLED 状态。**幂等**：连续两次调用是 no-op，
但响应里的 `changed` 字段会反映状态是否真的翻转。

与 `enable_collision_detection` 对称；二者合起来对应 Dev 面板
`MS_OT_dev_toggle_collision` 按钮（按当前状态切换）。

**请求**
```json
{"id": 6, "method": "disable_collision_detection", "params": {}}
```

**响应**
```json
{"ok": true, "id": 6, "result": {"enabled": false, "changed": true}}
```

**错误**
- `INVALID_PARAMS` — 引擎未构造。
- `INTERNAL_ERROR` — `engine.set_disabled` 抛异常。

---

### 4.6 `apply_command`

核心 method：向指定模块派发 action。

**请求**
```json
{
  "id": 4,
  "method": "apply_command",
  "params": {
    "module_id": "<module_id>",
    "action": "<该模块 VALID_ACTIONS 中的一个>",
    "payload": { /* 见各模块，§4.6.2 或 §6.x */ }
  }
}
```

**参数**
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `module_id` | string | 是 | 模块 id（与 `name` 取值相同） |
| `action` | string | 是 | 目标模块 `VALID_ACTIONS` 中的一个 |
| `payload` | object | 否 | action 专属参数，缺省 `{}` |

**响应（成功）**
```json
{"ok": true, "id": 4, "result": null}
```

**响应（失败）**
```json
{
  "ok": false,
  "id": 4,
  "error": {"code": "AXIS_NOT_FOUND", "message": "no module 'NoSuchAxis'"}
}
```

> **响应不携带 `method` / `module_id` / `action`**，客户端必须靠请求时自维护的 `id` 做关联。
>
> **RPC 层不校验 action 词表**：action 是否合法由各模块 `apply_command` 自己判断，
> 未知 action **静默忽略并返回 `ok=true`**（拼错 action 会“看起来成功”，见 §9 坑 10）。
> 只有 `module_id` 不存在才报 `AXIS_NOT_FOUND`；轴类 BLOCKED 时报 `AXIS_BLOCKED`。

#### 4.6.1 `VALID_ACTIONS`（按模块）

| kind（模块） | `VALID_ACTIONS` | 源码位置 |
|---|---|---|
| `linear_axis` / `rotate_axis` | `idle`, `home`, `jog`, `move_to`, `velocity`, `stop`（**6 种，不含 `reset_collision`**） | `modules/LinearAxix/axis.py`、`modules/RotateAxis/rotate_axis.py` |
| `conveyor` | `set_running`, `set_speed`, `set_direction`, `set_friction`, `start`, `stop`, `idle`, `reset_collision`（8 种） | `modules/Conveyor/runtime.py` |
| `cylinder` | `set_outputs`, `reset_collision`, `stop`, `idle`（4 种） | `modules/Cylinder/cylinder.py` |
| `vacuum_nozzle` | `set_enabled`, `force_release`, `idle`, `stop`, `reset_collision`（5 种） | `modules/VacuumNozzle/runtime.py` |
| `approach_sensor` | `reset_collision`（1 种） | `modules/ApproachSensor/runtime.py` |

> **`reset_collision` 不是轴类的 action** —— 它是 collision engine 的系统级操作，
> 严禁向 LinearAxis / RotateAxis 发送（会被静默忽略，或在 BLOCKED 时得到
> `AXIS_BLOCKED`）。解除 BLOCKED 的路径见 §6.0。

#### 4.6.2 轴类（LinearAxis / RotateAxis）action 的 payload

| `action` | LinearAxis payload | RotateAxis payload | 物理含义（LinearAxis） | 物理含义（RotateAxis） |
|----------|---------------------|---------------------|------------------------|-------------------------|
| `move_to` | `{target_x, velocity?}` | `{target_angle, duration_s?}` | `target_x` 当 BU 用；`velocity` = BU/s（RPC 路径**不做 mm→BU 转换**） | `target_angle` = deg；`duration_s` = P2P 总秒数 |
| `jog` | `{direction, velocity}` | `{direction, velocity}` | `direction ∈ {-1, +1}`；`velocity` = 幅度（绝对值）；最终 `direction * \|velocity\|` BU/s | `direction ∈ {-1, +1}`；`velocity` = 幅度（绝对值）；最终 `direction * \|velocity\|` deg/s |
| `home` | `{direction?, velocity?}` | `{direction?, velocity?}` | `direction` 默认 `host.linear_axis.home_direction`（再缺省 `-1`）；`velocity` = **mm/s**，默认 `host.linear_axis.home_speed`（再缺省 `0.1`）。RPC 与 UI 走同一份 `axis_ops.send_command` 代码（mm→BU 换算在里面完成）。 | `direction` 默认 `host.rotate_axis.home_direction`（再缺省 `-1`）；`velocity` = **deg/s**，默认 `host.rotate_axis.angular_home_speed`（再缺省 `10.0`）。RPC 与 UI 走同一份 `axis_ops.send_command` 代码（角度域无单位换算）。 |
| `velocity` | `{velocity}` | `{velocity}` | BU/s，直接给 `set_velocity(v)` | deg/s，给 `set_velocity(omega)` |
| `stop` | `{}` | `{}` | `slider.start_idle()`，`stop_reason="cmd_stop"` | 同 |
| `idle` | `{}` | `{}` | 等价 stop，`stop_reason="cmd_idle"` | 同 |
| `reset_collision` | **不接受** | **不接受** | 见 §6.1 / §6.2 / §6.0 | 同 |

> `home` 两边都走 axis_ops.send_command（mm→BU 或角度域无换算都在里面完成），与 UI 同源。其他 LinearAxis / RotateAxis 命令（`move_to` / `velocity` / `jog`）仍走 RPC 直调路径，`target_x` / `target_angle` / `velocity` 按协议单位直给（LinearAxis 的 BU/s、RotateAxis 的 deg/s）—— 若客户端期望 mm/s（LinearAxis），需自行乘 `_scene_linear_mm_to_bu_factor()`。
> RotateAxis 角度域不涉及 `scale_length`，无换算问题。
>
> 其余模块（Conveyor / Cylinder / VacuumNozzle / ApproachSensor）的 action
> payload 见 §6 对应模块小节。

---

### 4.7 `set_outputs`

设置 `Cylinder` 模块的外部控制信号 `Output_1` / `Output_2`。

Cylinder 运行时每 tick 读 `host.cylinder.output_1` / `output_2` 决定目标位置:
- `(T, F)` → 去工作位置 1
- `(F, T)` → 去工作位置 2
- `(T, T)` / `(F, F)` → 非法组合,保持 IDLE

本 method 写入后下个 tick 自然 reconcile 到 IDLE / MOVING_TO_<target>,
不需要再发 `apply_command`。本 method **直接调 setter，不经 apply_command
的 BLOCKED 门控**（Cylinder BLOCKED 期间本 method 仍生效）。

**请求**
```json
{
  "id": 7,
  "method": "set_outputs",
  "params": {
    "module_id": "Cylinder1",
    "output_1": true,
    "output_2": false
  }
}
```

**参数**
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `module_id` | string | 是 | Cylinder host 名(例:`"Cylinder1"`) |
| `output_1` | bool | 是 | 输出信号位 1 |
| `output_2` | bool | 是 | 输出信号位 2 |

**响应**
```json
{
  "ok": true,
  "id": 7,
  "result": {"output_1": true, "output_2": false}
}
```

**错误**
- `INVALID_PARAMS` —— `module_id` 非字符串,或 `output_1` / `output_2` 不是 bool
- `AXIS_NOT_FOUND` —— `module_id` 不在 manager 里,或 module 不支持 `set_outputs`(非 Cylinder)

**语义说明**
- `output_1` / `output_2` 写到 module instance + host CylinderProperty,
  与 Dev 面板的 toggle 同步。
- 写入后**不**改变当前 `state`;下一个 tick 由 module 自己决定是 IDLE 还是 MOVING_TO。
- 多次写同一对值是幂等的(响应内容一致，但每次都会重写 cfg)。

---

### 4.8 `set_vacuum_enabled`

真空吸嘴的 On/Off 开关。对应 host 上的
`Object.vacuum_nozzle.enabled`（面板上的 “Vacuum On” toggle）。
**直接调 setter，不经 apply_command 的 BLOCKED 门控。**

**请求**
```json
{
  "id": 12,
  "method": "set_vacuum_enabled",
  "params": {"module_id": "VacuumNozzle1", "enabled": true}
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `module_id` | string | 是 | VacuumNozzle host 名 |
| `enabled` | bool | 是 | `true` = On（开真空），`false` = Off（关真空，默认态） |

**响应**
```json
{"ok": true, "id": 12, "result": {"enabled": true}}
```

**错误**
- `INVALID_PARAMS` —— `module_id` 非字符串，或 `enabled` 不是 bool
- `AXIS_NOT_FOUND` —— `module_id` 不在 manager 里，或 module 不支持 `set_enabled`

**语义说明**
- **On**：写 cfg + instance。下一个 tick 扫描感应区，把区内的
  “可吸物体” 变成吸嘴的子对象（吸嘴跟着轴运动时它们跟着走）。
- **Off**：**立即**把持有的子对象全部拆解并放回场景目录
  （`parent = None`，保留**当前** world 位姿），不必等下一个 tick。
- 吸住是 **sticky** 的：一旦吸住就一直持有，只有 Off / `force_release`
  才释放。
- Off 与 `force_release` 都不是运动，所以 **BLOCKED 期间也会执行**。

---

### 4.9 `force_release_vacuum`

立即强制释放真空吸嘴当前持有的全部物体（应急出口）。
**直接调 setter，不经 apply_command 的 BLOCKED 门控。**

**请求**
```json
{"id": 13, "method": "force_release_vacuum", "params": {"module_id": "VacuumNozzle1"}}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `module_id` | string | 是 | VacuumNozzle host 名 |

**响应**
```json
{"ok": true, "id": 13, "result": {"released": true}}
```

`released` 为 `true` 当且仅当调用前确实持有至少一个物体。

**错误**
- `INVALID_PARAMS` —— `module_id` 非字符串
- `AXIS_NOT_FOUND` —— `module_id` 不在 manager 里，或 module 不支持 `force_release`

**语义说明**
- **绕过 BLOCKED 门控** —— 碰撞锁定时也能丢件，避免工件被永久粘住。
- 不改 `state`（仍可能是 `blocked`），只清空持有列表。

> 与 `set_outputs` 对称：真空的两个控制信号走独立的顶层 method，
> 客户端不需要知道 VacuumNozzle 内部的 action 词表。

---

### 4.10 `set_conveyor_running`

启停 / 调速 / 调方向 / 调摩擦 于一个 Conveyor module。Conveyor 与
LinearAxis / RotateAxis 共享 `category="axes"`（在 state_push 中出
现在 `data["axes"]` 里），但 kind 不同；本 method 是 Conveyor 专门的
顶层 RPC，与 `set_outputs` / `set_vacuum_enabled` 同构。**直接调 setter，
不经 apply_command 的 BLOCKED 门控。**

**请求**
```json
{
  "id": 14,
  "method": "set_conveyor_running",
  "params": {
    "module_id": "Conveyor1",
    "running": true,
    "direction": 1,
    "target_speed": 50.0,
    "friction": 1.0
  }
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `module_id` | string | 是 | Conveyor host 名（如 `"Conveyor1"`） |
| `running` | bool | 否 | 是否启动；缺省不改 |
| `direction` | int ∈ {-1, +1} | 否 | 传送方向；1 = drive → idler，-1 = idler → drive；缺省不改 |
| `target_speed` | number ≥ 0 | 否 | 线速度；数值**直接当 BU/s 使用**（1 BU = 1 mm 的场景下与 mm/s 等值，运行时不做 mm→BU 换算，见 §9 坑 6）；缺省不改 |
| `friction` | number ∈ [0.0, 1.0] | 否 | 抓地系数；缺省不改 |

**响应**
```json
{"ok": true, "id": 14, "result": {"running": true, "direction": 1, "target_speed": 50.0, "friction": 1.0}}
```

响应里的字段是本次生效的最终值（从 instance snapshot 读，所以也反映
了其它 set_* 路径写入的最新状态）。

**错误**
- `INVALID_PARAMS` —— `module_id` 非字符串；`running` 不是 bool；
  `direction` 不是 -1/+1；`target_speed` 不是数字或 < 0；
  `friction` 不是数字或 ∉ [0, 1]。
- `AXIS_NOT_FOUND` —— `module_id` 不在 manager 里，或 module 不
  是 `kind="conveyor"`。

**语义说明**
- 所有参数都是可选的;缺省参数不改对应字段,方便"只改速度"或
  "只改方向"这种局部修改。
- **等价 action**:通用 `apply_command` 的 `action="start"` 与本
  method 等价 —— payload 字段同名(`target_speed` / `direction` /
  `friction`,缺省不改),唯一差异是 `start` 的 `running` 缺省为
  `true`。走通用命令通道时用它。
- `running=False` 立即生效（下一 tick `update()` 第 5 步短路，不扫场景、
  不写 linear_velocity）；uv_offset 仍按当前速度累加（即
  `running=False` 时 speed 也为 0，uv 不变；artist 调过速度后再 stop
  设置仍保留）。
- `direction` / `target_speed` / `friction` 修改立即写入 cfg + instance，
  下一 tick 扫场景按新值计算。
- **不被 BLOCKED 门控拒**：状态位类（`set_running` / `set_speed` /
  `set_direction` / `set_friction`）允许在 BLOCKED 期间写入。如果需要
  "BLOCKED 时强制停"，同样可以通过通用 `apply_command(module_id,
  action="stop")` 走 BLOCKED 守卫。
- 缺省参数语义上的"局部修改"使得"只改速度不影响 running 状态"成为可
  能；旧版未提供类似 fallback，artist 调速度必须连 running 一起发。

---

## 5. state_push 数据格式

> ⚠ **BREAKING**（与旧版相比）：`cylinder` 模块不再出现在 `data["axes"]`
> 里 —— 它现在独立放在 `data["cylinders"]`。RPC 客户端如果原本从
> `data["axes"]` 找 cylinder，必须改成读 `data["cylinders"]`。
> `approach_sensor` 模块（旧名 `sensor`）独立放在 `data["approach_sensors"]`。

每条推流是一行 JSON：
```json
{
  "type": "state_push",
  "data": {
    "axes": [
      { "module_id": "LinearAxisX", "name": "LinearAxisX", "kind": "linear_axis", ... },
      { "module_id": "RotateAxis.R", "name": "RotateAxis.R", "kind": "rotate_axis", ... },
      { "module_id": "Conveyor1", "name": "Conveyor1", "kind": "conveyor", "running": true, ... }
    ],
    "cylinders": [
      { "module_id": "Cylinder1", "name": "Cylinder1", "kind": "cylinder", ... }
    ],
    "vacuum_nozzles": [
      { "module_id": "VacuumNozzle1", "name": "VacuumNozzle1", "kind": "vacuum_nozzle", ... }
    ],
    "approach_sensors": [
      { "module_id": "ApproachSensor1", "name": "ApproachSensor1", "kind": "approach_sensor", ... }
    ],
    "collision": { "category": "collision", "enabled": true, "active": false, ... }
  }
}
```

`data` 的顶层 key 分布：

| key | 内容 | 是否必出现 |
|---|---|---|
| `axes` | `linear_axis` + `rotate_axis` + `conveyor` 的所有 module | 仅当至少有一个时 |
| `cylinders` | `cylinder` 的所有 module | 仅当至少有一个 cylinder 时 |
| `vacuum_nozzles` | `vacuum_nozzle` 的所有 module | 仅当至少有一个 vacuum nozzle 时 |
| `approach_sensors` | `approach_sensor` 的所有 module | 仅当至少有一个 approach sensor 时 |
| `collision` | 单例 collision 引擎的状态 | **恒出现**，即使没有任何 module |

桶之间的分类由 :attr:`BaseSimulationModule.category` 决定 —— 不是 `kind`。
所以将来若新增 kind 但复用现有 category（例如某种新轴仍然归到 `"axes"`），
不需要改这里；反之若某种新模块要单独成桶，给它一个新的 category 即可。

> **client 写法**：任意一个 category 都可能缺席（没模块时整条 key 不出现）。
> `data` 用 `data.get("axes") or []` / `data.get("cylinders") or []` /
> `data.get("vacuum_nozzles") or []` / `data.get("approach_sensors") or []` 取，**不要**
> 直接索引 `data["axes"]`；`collision` 那一项除外（它必在）。
>
> 每条 snapshot 按 `kind` 分支处理（字段表见 §5.2，功能说明见 §6）。

### 5.1 collision 块（顶层）

碰撞引擎是 **场景级单例**（不是 per-axis module），所以不参与
`{category: [snapshots, ...]}` 的 fan-out。`SimulationManager.snapshot()`
会在顶层附加一个单独的 `"collision"` 块，反映当前引擎状态：

| 字段 | 类型 | 说明 |
|------|------|------|
| `category` | string | 固定 `"collision"`，方便客户端判别顶层 key 含义 |
| `enabled` | bool | `engine.is_enabled`；与 Dev 面板的 "ENABLED/DISABLED" 状态文字一致 |
| `active` | bool | `engine.read_marker()`：任一轴被 BLOCKED 即为 true |
| `obstacle_count` | int | 已缓存的非轴障碍 BVH 数（`engine.obstacle_count`） |
| `axis_bvh_count` | int | 已缓存的 per-axis BVH 数（`engine.axis_bvh_count`） |
| `last_collision` | dict \| null | 最近一次碰撞记录（`axis_id` / `obstacle_name` / `slider_name`），无则为 null |

> `enabled == false` 时 `active` 始终为 false（`read_marker` 在禁用状态下
> 恒 False），与是否设置了 stale scene flag 无关。
>
> 客户端无须做 `if "collision" in data` 判断：即使引擎尚未构造
> （addon 刚 `register()` 但 `_init_once` 还没跑），`"collision"` 字段
> 也总是出现，且 schema 完整。

### 5.2 snapshot 字段全集（按模块）

| 模块 | 字段表小节 | 对应功能说明 |
|---|---|---|
| LinearAxis | [LinearAxis snapshot](#linearaxis-snapshot) | §6.1 |
| RotateAxis | [RotateAxis snapshot](#rotateaxis-snapshot) | §6.2 |
| Cylinder | [Cylinder snapshot](#cylinder-snapshot) | §6.4 |
| VacuumNozzle | [VacuumNozzle snapshot](#vacuumnozzle-snapshot) | §6.5 |
| ApproachSensor | [ApproachSensor snapshot](#approachsensor-snapshot) | §6.6 |
| Conveyor | [Conveyor snapshot](#conveyor-snapshot) | §6.3 |

#### LinearAxis snapshot
| 字段 | 类型 | 单位 | 说明 |
|------|------|------|------|
| `module_id` | string | — | 模块 id（与 `name` 取值相同） |
| `kind` | string | — | 固定 `"linear_axis"` |
| `name` | string | — | 轴名（原 `axis_id` 字段，RPC 已统一改名为 `name`） |
| `state` | string | — | 见 §5.3 |
| `home_done` | bool | — | 是否已完成 home |
| `current_x` | float | **mm** | 相对 logical origin 的位移（× `_bu_to_mm`） |
| `velocity` | float | **mm/s** | slider 当前速度（× `_bu_to_mm`） |
| `stop_reason` | string | — | 停止原因（`""`、`"homed"`、`"limit"`、`"arrived"`、`"cmd_stop"`、`"cmd_idle"`、`"collision"`） |
| `home_sensor` | bool | — | 任意 `kind=="home"` 的 sensor 触发 |
| `pos_limit_sensor` | bool | — | 任意 `kind=="back_limit"`（LinearAxis 的 positive limit）的 sensor 触发 |
| `neg_limit_sensor` | bool | — | 任意 `kind=="front_limit"`（LinearAxis 的 negative limit）的 sensor 触发 |

#### RotateAxis snapshot
| 字段 | 类型 | 单位 | 说明 |
|------|------|------|------|
| `module_id` | string | — | 模块 id（与 `name` 取值相同） |
| `kind` | string | — | 固定 `"rotate_axis"` |
| `name` | string | — | 轴名（原 `axis_id` 字段，RPC 已统一改名为 `name`） |
| `state` | string | — | 见 §5.3 |
| `home_done` | bool | — | 是否完成 home |
| `current_angle` | float | **deg** | 相对 zero_offset 的角度 |
| `velocity` | float | **deg/s** | rotator 当前角速度 |
| `stop_reason` | string | — | 同 LinearAxis |
| `home_sensor` | bool | — | 任意 `kind=="home"` 的 sensor 触发 |
| `pos_limit_sensor` | bool | — | 任意 `kind=="pos_limit"` 的 sensor 触发 |
| `neg_limit_sensor` | bool | — | 任意 `kind=="neg_limit"` 的 sensor 触发 |

> sensor 三个布尔字段由 `modules/components/sensor/base_sensor.py::aggregate_axis_sensor_states` 统一聚合，多个同类型 sensor 按 OR 聚合。

#### Cylinder snapshot

| 字段 | 类型 | 说明 |
|------|------|------|
| `module_id` | string | Cylinder host 名(同 `name`) |
| `kind` | string | 固定 `"cylinder"` |
| `name` | string | 同 `module_id` |
| `state` | string | runtime state: `"idle"` / `"moving_to_1"` / `"moving_to_2"` / `"blocked"` |
| `current_state` | string | 位置状态: `"approach_sensor_1"` / `"approach_sensor_2"` / `"unknown"`(两 sensor 都未触发) |
| `approach_sensor_1` | bool | `sensor_1.is_triggered` 的镜像 |
| `approach_sensor_2` | bool | `sensor_2.is_triggered` 的镜像 |
| `output_1` | bool | 输出信号位 1 |
| `output_2` | bool | 输出信号位 2 |
| `stop_reason` | string | 停止原因(`"arrived_at_sensor"` / `"cmd_stop"` / `"cmd_idle"` / `"no_target"` / `"collision"` 等) |

`state` 与 `current_state` / `approach_sensor_1/2` 三个独立维度:前者反映
runtime 行为(动还是停),后两者反映当前物理位置。`current_state` 是聚合
后的可选状态字符串(sensor 1 优先);`approach_sensor_1/2` 是底层 sensor 的
原始 bool,需要同时读两个 sensor 时走这个。位置状态由两 approach sensor 的
`is_triggered` 决定;两 sensor 同时触发时 `current_state` 取 1,两个 bool 都为 True。

不再单独暴露 `current_position` / `target_position`(int 1/2/None)以及
`current_x` / `velocity`(BU 位移 / 有符号速度)—— 位移 / 速度只是
runtime 的中间状态,客户端能且只需从 `state` + `current_state` +
两 sensor bool 推出当前阶段。

详见 `doc/CYLINDER.md`。

#### VacuumNozzle snapshot

| 字段 | 类型 | 说明 |
|------|------|------|
| `module_id` | string | VacuumNozzle host 名（同 `name`） |
| `kind` | string | 固定 `"vacuum_nozzle"` |
| `name` | string | 同 `module_id` |
| `state` | string | runtime state: `"disabled"`（Off）/ `"idle"`（On，未吸住）/ `"holding"`（On，已吸住）/ `"blocked"` |
| `enabled` | bool | 真空 On/Off（`true` = On） |
| `anchor_name` | string | 感应区锚点 —— **就是 host 自己** |
| `holding` | bool | 是否正在吸住至少一个物体 |
| `held_count` | int | 当前吸住的对象数量 |
| `held_names` | string[] | 当前吸住的对象名列表 |
| `sensing` | bool | 实时几何查询：感应区内当前是否有可吸物体（**与 On/Off 无关**，Off 时也可能为 `true`） |
| `stop_reason` | string | 停止原因(`""` / `"disabled"` / `"no_target"` / `"holding"` / `"force_release"` / `"collision"`) |

**兼容性变更（相对旧版）**：旧 snapshot 里的 `sensor_mesh_name` 与
`trigger_obj_name` 都已移除 —— 感应区直接锚定在 host 自己身上（锚点名就是
`anchor_name`），而 `trigger_obj` 这个“只吸指定对象”的白名单字段已被彻底
删除（它会静默绕过自动扫描，详见 `doc/VACUUM_NOZZLE.md` §4.1）。
现在可吸对象完全由运行时自动扫描决定。

详见 `doc/VACUUM_NOZZLE.md`。

#### ApproachSensor snapshot

| 字段 | 类型 | 说明 |
|------|------|------|
| `module_id` | string | sensor host mesh 名（同 `name`） |
| `kind` | string | 固定 `"approach_sensor"` |
| `name` | string | 同 `module_id` |
| `state` | string | runtime state: `"active"`（扫描中）/ `"disabled"`（cfg.enabled=False）/ `"blocked"` |
| `is_triggered` | bool | 本 tick 树形 AABB 剪枝 + SAT 扫描结果（任一 mesh 与感应盒相交） |
| `triggered_obj_name` | string | 第一个命中 mesh 的对象名（空字符串表示无命中） |
| `cube_size` | (float, float, float) | 感应盒尺寸 (l, w, h)（sensor 局部系，中心对齐） |
| `working_face_center` | (float, float, float) | 感应盒中心偏移（sensor 局部系） |
| `stop_reason` | string | 恒为空字符串（本 module 无 stop_reason 语义） |

功能与几何模型详见 §6.6 / `modules/ApproachSensor/runtime.py`。

#### Conveyor snapshot

| 字段 | 类型 | 说明 |
|------|------|------|
| `module_id` | string | Conveyor host 名（同 `name`） |
| `kind` | string | 固定 `"conveyor"` |
| `name` | string | 同 `module_id` |
| `state` | string | runtime state: `"idle"`（未启动）/ `"running"`（启动中）/ `"blocked"` |
| `running` | bool | 瞬时镜像 cfg.running（**判断启停以此为准**，见 §6.3） |
| `direction` | int | `+1` forward（drive → idler） / `-1` reverse |
| `target_speed` | number | 目标线速度（数值直接当 BU/s 用，1 BU = 1 mm 场景下数值同 mm/s） |
| `friction` | number | 抓地系数 ∈ [0, 1]；1.0 = 完全抓地，0.0 = 完全打滑 |
| `driven_names` | string[] | 本 tick 扫描到的被驱动物体名列表（数量 = `len(...)`） |
| `stop_reason` | string | `""` / `"running"` / `"cmd_stop"` / `"cmd_idle"` / `"invalid_direction"` / `"collision"` |

> **带宽裁剪**：`belt_dir` / `belt_speed_world` / `uv_offset` / `driven_count`
> 已从 state_push 移除 —— 内部状态仍照常维护，Dev 面板与 Conveyor 面板
> 直接读实例值显示，行为不变。
>
> 功能说明与 action 词表见 §6.3；专属 method 见 §4.10。

### 5.3 `state` 取值

轴类（`axis.py` / `rotate_axis.py` 顶部常量，LinearAxis 与 RotateAxis 共用）：

| 值 | 含义 |
|----|------|
| `"idle"` | 静止（无运动） |
| `"homing"` | 正在回 home（定速向 home sensor 方向） |
| `"moving_p2p"` | 点到点运动中 |
| `"moving_vel"` | 定速运动中 |
| `"stopped_at_limit"` | 撞到 limit sensor 或 rail clamp 后停在限位 |
| `"blocked"` | 撞障碍被 collision engine 锁住，**必须**解除才能继续（见 §6.0） |

Cylinder（`modules/Cylinder/cylinder.py`）：

| 值 | 含义 |
|----|------|
| `"moving_to_1"` | 向工作位置 1（approach_sensor_1）运动中 |
| `"moving_to_2"` | 向工作位置 2（approach_sensor_2）运动中 |
| `"idle"` | 不动（初始 / 已在目标位置 / outputs 非法组合） |
| `"blocked"` | 同上，必须解除 |

Conveyor（`modules/Conveyor/runtime.py`）：

| 值 | 含义 |
|----|------|
| `"idle"` | 未启动（`running=false`） |
| `"running"` | 启动中（`running=true`） |
| `"blocked"` | 同上，必须解除 |

VacuumNozzle（`modules/VacuumNozzle/runtime.py`）：

| 值 | 含义 |
|----|------|
| `"disabled"` | 真空 Off（默认态） |
| `"idle"` | On，未吸住 |
| `"holding"` | On，已吸住至少一个物体 |
| `"blocked"` | 碰撞锁定 |

ApproachSensor（`modules/ApproachSensor/runtime.py`）：

| 值 | 含义 |
|----|------|
| `"active"` | 扫描中 |
| `"disabled"` | `cfg.enabled = False` |
| `"blocked"` | 碰撞锁定 |

---

## 6. 按模块功能速查

> 每个模块一小节：**功能 → 控制入口 → BLOCKED 行为 → 数据字段**，
> 自包含可跳读。method 参数细节仍在 §4，snapshot 字段表在 §5.2，
> 本章给出行为语义与交叉指针。

### 6.0 模块总览 + 解除 BLOCKED 路径

| 模块 | kind | state_push 桶 | 专属 method（§4） | `apply_command` action 数 | BLOCKED 时 apply_command 行为 | 章节 |
|---|---|---|---|---|---|---|
| LinearAxis | `linear_axis` | `axes` | 无 | 6（**无 reset_collision**） | **全部拒绝 → `AXIS_BLOCKED` 错误** | §6.1 |
| RotateAxis | `rotate_axis` | `axes` | 无 | 6（**无 reset_collision**） | **全部拒绝 → `AXIS_BLOCKED` 错误** | §6.2 |
| Conveyor | `conveyor` | `axes` | `set_conveyor_running`（§4.10） | 8 | 6 种放行，`stop`/`idle` 静默丢弃 | §6.3 |
| Cylinder | `cylinder` | `cylinders` | `set_outputs`（§4.7） | 4 | 放行 `set_outputs`/`reset_collision`，其余静默丢弃 | §6.4 |
| VacuumNozzle | `vacuum_nozzle` | `vacuum_nozzles` | `set_vacuum_enabled`/`force_release_vacuum`（§4.8/4.9） | 5 | 放行 `set_enabled`/`force_release`/`reset_collision`，`idle`/`stop` 静默丢弃 | §6.5 |
| ApproachSensor | `approach_sensor` | `approach_sensors` | 无 | 1（`reset_collision`） | 放行 `reset_collision` | §6.6 |

**BLOCKED 通用语义**：碰撞引擎写的是**场景级** marker
（`scene["motion_simulation_collision"]`），任一模块命中 → **所有**模块
下一 tick 进 `blocked`。解除（`reset_collision` 语义）有三条等效路径：

1. **给任意收 `reset_collision` 的模块发 `apply_command`**
   （Cylinder / VacuumNozzle / Conveyor / ApproachSensor）——
   清 scene 级 marker，全体解锁；
2. **`enable_collision_detection`（§4.4）** —— `set_disabled` 无论开关
   都会先 `clear_collision()`；**这是只有轴类的场景里唯一可靠的 RPC 解法**；
3. **`disable_collision_detection`（§4.5）** —— 清 marker 并**立即**
   （不等下一 tick）强制释放所有 BLOCKED 模块。

> 轴类（LinearAxis / RotateAxis）**不接受** `reset_collision` action：
> 发给它们会被忽略（idle 时 `ok=true` 无效果），BLOCKED 时得到
> `AXIS_BLOCKED` 错误（错误信息指向 `CollisionEngine.clear_collision()`）。
> Python API 侧才可直接 `module.reset_collision()` / `engine.clear_collision()`。

---

### 6.1 LinearAxis（直线轴）

**功能**：slider 沿 rail 做一维直线运动 —— 定点（`move_to`）、定速
（`velocity` / `jog`）、回零（`home`）。运动被 home / limit sensor 与
碰撞引擎双重约束；没有专属顶层 method，全部走 `apply_command`。

**控制入口**：`apply_command`（§4.6），action = `idle` / `home` / `jog` /
`move_to` / `velocity` / `stop`；payload 详见 §4.6.2 左列。

**单位要点**：
- `target_x`、`velocity`（move_to / jog / velocity）按 **BU** 直给 ——
  RPC 路径**不做** mm→BU 换算（snapshot 反馈是 mm，见下）；
- `home.velocity` 按 **mm/s** 给（走 `axis_ops.send_command`，内部换算；
  缺省取 host cfg `home_speed`）；
- snapshot 的 `current_x` / `velocity` 是 **mm / mm·s⁻¹**（内部 BU ×
  `_bu_to_mm`）。换算系数：`BU = mm × 0.001 / scene.unit_settings.scale_length`。

**BLOCKED 行为**：`apply_command` 入口处对**任何** action 直接抛
`AxisBlockedError` → RPC `AXIS_BLOCKED`（见 §7）。解除路径见 §6.0。

**数据字段**：snapshot → [§5.2 LinearAxis snapshot](#linearaxis-snapshot)；
`state` → §5.3 轴类六态表；`stop_reason` → `""` / `homed` / `limit` /
`arrived` / `cmd_stop` / `cmd_idle` / `collision`。

**注意**：`stop_reason == "collision"` + `state == "blocked"` 表示撞上
外部障碍；先按 §6.0 解除，再发后续命令。

---

### 6.2 RotateAxis（旋转轴）

**功能**：rotator 绕 RotateCenter 指定轴做旋转运动 —— 定角
（`move_to`）、定角速度（`velocity` / `jog`）、回零（`home`）。结构与
LinearAxis 同构（同 action 词表、同 BLOCKED 行为、同 snapshot 骨架）。

**控制入口**：`apply_command`（§4.6），action = `idle` / `home` / `jog` /
`move_to` / `velocity` / `stop`；payload 详见 §4.6.2 右列。

**单位要点**：角度域全部 **deg / deg·s⁻¹**，不涉及 `scale_length`、
无换算问题。`move_to` 用 `target_angle` + `duration_s`（**不是**
`velocity` —— 与 LinearAxis 同名 action 参数语义不同，见 §9 坑 8）。

**BLOCKED 行为**：与 LinearAxis 完全一致 —— 任何 action 抛
`AxisBlockedError` → `AXIS_BLOCKED`；`reset_collision` 不是 action。
解除路径见 §6.0。

**数据字段**：snapshot → [§5.2 RotateAxis snapshot](#rotateaxis-snapshot)；
`state` → §5.3 轴类六态表；`stop_reason` 取值同 LinearAxis。

---

### 6.3 Conveyor（传送带）

**功能**：不产自身运动 —— 每 tick 把接触 belt 的**外部刚体**沿
`belt_dir` 推动（写 `linear_velocity`），并向 belt mesh 写
`belt_uv_offset` 自定义属性驱动材质滚动。运动状态由
`running / direction / target_speed / friction` 四个信号位描述。

**控制入口**（两条，等效）：

1. **专属 method** `set_conveyor_running`（§4.10）—— 推荐；
   所有参数可选、缺省不改、返回生效终值。
2. **`apply_command`**（§4.6），8 种 action：

| action | payload | 说明 |
|---|---|---|
| `set_running` | `{running: bool}` | 启停 |
| `set_speed` | `{target_speed: ≥0}` | 线速度（BU/s 语义，见 §4.10 参数表） |
| `set_direction` | `{direction: ±1}` | 1 = drive → idler |
| `set_friction` | `{friction: 0..1}` | 抓地系数 |
| `start` | `{target_speed?, direction?, friction?, running?}` | **≡ `set_conveyor_running(running=true, ...)`**：字段同名同序、缺省不改，唯一差异 `running` 缺省 `true` |
| `stop` | `{}` | `set_running(False)` + `stop_reason="cmd_stop"` |
| `idle` | `{}` | 同 stop，`stop_reason="cmd_idle"` |
| `reset_collision` | `{}` | 清 scene 级碰撞 marker |

**BLOCKED 行为**：放行 `set_running` / `set_speed` / `set_direction` /
`set_friction` / `start` / `reset_collision`（状态位待遇：只写信号位，
`state` 不变，解锁后按 `running` 恢复）；`stop` / `idle` **静默丢弃**
（`ok=true` 但无效）。专属 method 不经门控，BLOCKED 期间照常可用。

**数据字段**：snapshot → [§5.2 Conveyor snapshot](#conveyor-snapshot)；
`state` → §5.3 Conveyor 三态表；`stop_reason` → `""` / `running` /
`cmd_stop` / `cmd_idle` / `invalid_direction` / `collision`。

**已知注意**：
- **判断启停以 `running` 为准**：`update()` 每 tick 把 `cfg.running`
  镜像进 instance，但**不改** `state`；若 panel 勾选未走 Apply（或 RNA
  直写 cfg），会出现 `state="running"` 而 `running=false` 的漂移态，
  此时以 `running` 为准（`state` 只用于识别 `blocked`）。
- `uv_offset` 永远累加，Start/Stop 不重置（面板可见，不在推流里）。
- `target_speed` 直接当 BU/s 用，无 mm→BU 换算因子。

---

### 6.4 Cylinder（气缸）

**功能**：双位直线气缸 —— 读 `output_1` / `output_2` 输出对决定目标
位置，沿两 approach sensor 连线方向推动 `work_bar`
（`moving_to_1` / `moving_to_2`），到位由 sensor 触发判定。

**控制入口**（两条）：

1. **专属 method** `set_outputs`（§4.7）—— `{"output_1": bool, "output_2": bool}`，
   直接调 setter、不经门控；
2. **`apply_command`**（§4.6），4 种 action：

| action | payload | 说明 |
|---|---|---|
| `set_outputs` | `{output_1: bool, output_2: bool}` | 同 §4.7；`(T,F)`→位置1，`(F,T)`→位置2，其余非法保持 idle |
| `stop` | `{}` | 立停，`stop_reason="cmd_stop"` |
| `idle` | `{}` | 同 stop，`stop_reason="cmd_idle"` |
| `reset_collision` | `{}` | 清 scene 级碰撞 marker |

**BLOCKED 行为**：放行 `set_outputs` / `reset_collision`；`stop` / `idle`
静默丢弃。专属 method 不经门控。

**数据字段**：snapshot → [§5.2 Cylinder snapshot](#cylinder-snapshot)；
`state` → §5.3 Cylinder 四态表；`stop_reason` → `arrived_at_sensor` /
`cmd_stop` / `cmd_idle` / `no_target` / `collision`。

详见 `doc/CYLINDER.md`。

---

### 6.5 VacuumNozzle（真空吸嘴）

**功能**：On 后每 tick 扫描感应区（锚定 host 自身的盒），把区内可吸
mesh reparent 成吸嘴子对象（**sticky**：吸住就一直持有，吸嘴随轴运动
工件跟随）；Off / `force_release` 立即解除全部父子关系并保留 world
位姿放回场景。

**控制入口**（两条）：

1. **专属 method**（推荐，直接调 setter、**不经门控**）：
   - `set_vacuum_enabled`（§4.8）—— `{enabled: bool}`
   - `force_release_vacuum`（§4.9）—— `{}`，应急丢件
2. **`apply_command`**（§4.6），5 种 action：

| action | payload | 说明 |
|---|---|---|
| `set_enabled` | `{enabled: bool}` | 同 §4.8 |
| `force_release` | `{}` | 同 §4.9 |
| `idle` | `{}` | 等价 `set_enabled(False)` |
| `stop` | `{}` | 等价 `set_enabled(False)` |
| `reset_collision` | `{}` | 清 scene 级碰撞 marker |

**BLOCKED 行为**：放行 `set_enabled` / `force_release` /
`reset_collision`；`idle` / `stop` **静默丢弃**（改 state 的动作被拒）。
专属 method 不经门控。

**数据字段**：snapshot → [§5.2 VacuumNozzle snapshot](#vacuumnozzle-snapshot)
（`held_count` / `held_names` / `sensing` 语义注意：`sensing` 与 On/Off
无关）；`state` → §5.3 VacuumNozzle 四态表；`stop_reason` → `""` /
`disabled` / `no_target` / `holding` / `force_release` / `collision`。

详见 `doc/VACUUM_NOZZLE.md`。

---

### 6.6 ApproachSensor（被动接近传感器）

**功能**：纯被动感测器（不产生任何运动）—— host 就是 sensor mesh，
每 tick 用树形 AABB 剪枝 + SAT 检测场景里是否有 mesh 与感应盒
（`cube_size` × `working_face_center`，sensor 局部系中心对齐盒）相交，
结果写进 `is_triggered` / `triggered_obj_name` 与 host 自定义属性。

**控制入口**：仅 `apply_command`（§4.6），唯一 action `reset_collision`
（payload `{}`）。**无业务动作** —— `stop` / `idle` / `set_outputs` 等
全部静默丢弃，不实现。

**BLOCKED 行为**：放行 `reset_collision`，其余静默丢弃。

**数据字段**：snapshot →
[§5.2 ApproachSensor snapshot](#approachsensor-snapshot)；
`state` → §5.3 ApproachSensor 三态表；`stop_reason` 恒 `""`。

**改名历史**：旧版叫 `Sensor`（`kind="sensor"` / `category="sensors"`），
与 LinearAxis 的 UTypeSensor（`sensor_direction` / `home_sensor` /
`pos_sensor` …）在面板里撞名，已重命名为 `ApproachSensor` /
`kind="approach_sensor"` / `category="approach_sensors"`；旧 `Sensor*`
host 名不再被认领，需手动改成 `ApproachSensor*`。

**盒模型**：感应盒 = sensor 局部系下中心对齐的轴对齐盒，中心
`working_face_center`、尺寸 `cube_size`（三维独立）；旧
`approach_sensor_normal_axis` 字段已删除。与 Cylinder 的
`approach_sensor_1/2` 共用同一套几何 helpers，区别仅在 trigger
（每 tick 扫全部场景 mesh，而非硬编码 trigger_obj）。

---

## 7. 错误码

| `error.code` | 含义 | 触发位置 |
|--------------|------|----------|
| `INVALID_REQUEST` | 请求不是合法 JSON / 类型错 | `_read_message`、`_handle_message` 入口 |
| `INVALID_PARAMS` | `params` 不是对象、`method` 不存在、`module_id` / `action` / `interval_ms` 等参数类型或取值错 | `_handle_message`、`_handle_apply_command`、`_handle_subscribe`、各专属 method handler |
| `METHOD_NOT_FOUND` | `method` 不在白名单 | `_handle_message` |
| `AXIS_NOT_FOUND` | `module_id` 不在 manager 里，或模块不支持该专属 method（**仅 RPC 层抛**；走 Python API 直接调 `manager.apply_command` 是静默 no-op） | `_handle_apply_command` 的 `dispatch` 闭包、各专属 method handler |
| `AXIS_BLOCKED` | **LinearAxis / RotateAxis** 在 `blocked` 状态下收到**任何** `apply_command`（包括 `reset_collision`）—— 轴类 `apply_command` 入口直接抛 `AxisBlockedError`；message 提示走 `CollisionEngine.clear_collision()`（RPC 面等效路径见 §6.0） | `_handle_apply_command` 的 `dispatch` 闭包 |
| `INTERNAL_ERROR` | 主线程 dispatch 抛任何异常（含 SimulationManager / 模块抛的），或 bridge 超时（默认 10s） | `_client_loop` 兜底 |

> 其它模块（Conveyor / Cylinder / VacuumNozzle / ApproachSensor）BLOCKED
> 期间收到被拒 action 时**不报错** —— 静默丢弃并返回 `ok=true`
> （见 §6 各模块 BLOCKED 行为）。

---

## 8. 客户端参考实现（Python）

```python
import socket
import json

HOST = '127.0.0.1'
PORT = 9877

def call_rpc(req: dict, timeout: float = 2.0) -> dict:
    """发送一条 RPC 请求并读取单行 JSON 响应。

    适用于 ``ping`` / ``subscribe_state`` / ``unsubscribe_state`` /
    ``apply_command`` 同步响应。state_push 推流应另开线程持续 ``recv``。
    """
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((HOST, PORT))
    try:
        s.sendall((json.dumps(req, separators=(',', ':')) + '\n').encode('utf-8'))
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b'\n' in buf:
                break
        line, _, _ = buf.partition(b'\n')
        return json.loads(line.decode('utf-8'))
    finally:
        s.close()


def pump_state_push(on_snapshot, timeout: float = 5.0):
    """长连接示例：订阅并持续处理 state_push 直到超时 / 出错。

    ``on_snapshot(dict)`` 会被每个 ``data`` 字段调用一次。
    """
    s = socket.socket()
    s.settimeout(timeout)
    s.connect((HOST, PORT))
    try:
        # 订阅
        s.sendall(
            (json.dumps({"id": 1, "method": "subscribe_state",
                         "params": {"interval_ms": 100}}) + '\n').encode('utf-8')
        )
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                if not line.strip():
                    continue
                msg = json.loads(line.decode('utf-8'))
                if msg.get('type') == 'state_push':
                    on_snapshot(msg['data'])
    finally:
        s.close()


# ---- 使用示例 ----

# 1) ping
print(call_rpc({"id": 1, "method": "ping", "params": {}}))
# → {"ok": true, "id": 1, "result": {"pong": true}}

# 2) 订阅推流（一次性设置，然后起线程 pump）
print(call_rpc({"id": 2, "method": "subscribe_state",
                "params": {"interval_ms": 50}}))

# 3) 启动 home
print(call_rpc({"id": 3, "method": "apply_command", "params": {
    "module_id": "LinearAxisX",
    "action": "home",
    "payload": {"direction": -1, "velocity": 2.0},
}}))

# 4) 点到点移动
print(call_rpc({"id": 4, "method": "apply_command", "params": {
    "module_id": "LinearAxisX",
    "action": "move_to",
    "payload": {"target_x": 30.0, "velocity": 5.0},
}}))

# 5) jog（按显式 direction 定速）
print(call_rpc({"id": 5, "method": "apply_command", "params": {
    "module_id": "LinearAxisX",
    "action": "jog",
    "payload": {"direction": -1, "velocity": 2.0},
}}))
# → slider.set_velocity(-2.0)  BU/s；state 转为 "moving_vel"

# 6) 停
print(call_rpc({"id": 6, "method": "apply_command", "params": {
    "module_id": "LinearAxisX",
    "action": "stop",
    "payload": {},
}}))

# 7) 解除碰撞阻塞 —— 轴类不收 reset_collision action(见 §6.0/§6.1)。
#    正规姿势:重新 enable 引擎(set_disabled 恒先 clear_collision);
#    或给任意收 reset_collision 的模块(Conveyor/Cylinder/...)发 action。
print(call_rpc({"id": 7, "method": "enable_collision_detection", "params": {}}))
# → {"ok": true, "id": 7, "result": {"enabled": true, "changed": false}}
#    changed=false 说明引擎本来就开着,但 marker 已被顺手清掉 →
#    被锁的模块下一 tick 自动解锁。AXIS_BLOCKED 之后重发原命令即可。
#
# 等效写法(场景里有 Conveyor 时):
# print(call_rpc({"id": 7, "method": "apply_command", "params": {
#     "module_id": "Conveyor1",
#     "action": "reset_collision",
#     "payload": {},
# }}))

# 8) 退订
print(call_rpc({"id": 8, "method": "unsubscribe_state", "params": {}}))

# 9) 停用碰撞检测（artist-facing kill switch：Dev 面板 "Disable" 按钮的 RPC 版本）
print(call_rpc({"id": 9, "method": "disable_collision_detection", "params": {}}))
# → {"ok": true, "id": 9, "result": {"enabled": false, "changed": true}}

# 10) 设置 Cylinder 输出位 — `(output_1=True, output_2=False)` 去工作位置 1
print(call_rpc({
    "id": 10,
    "method": "set_outputs",
    "params": {
        "module_id": "Cylinder1",
        "output_1": True,
        "output_2": False,
    },
}))
# → {"ok": true, "id": 10, "result": {"output_1": true, "output_2": false}}

# 11) 切换到工作位置 2
print(call_rpc({
    "id": 11,
    "method": "set_outputs",
    "params": {"module_id": "Cylinder1", "output_1": False, "output_2": True},
}))

# 12) 传送带起动(专属 method,参数可选)
print(call_rpc({
    "id": 12,
    "method": "set_conveyor_running",
    "params": {"module_id": "Conveyor1",
               "running": True, "direction": 1,
               "target_speed": 50.0, "friction": 1.0},
}))
# 等效的通用 action 写法(见 §6.3):
# print(call_rpc({"id": 12, "method": "apply_command", "params": {
#     "module_id": "Conveyor1",
#     "action": "start",
#     "payload": {"target_speed": 50.0, "direction": 1},
# }}))

# 13) 真空吸取 / 应急释放
print(call_rpc({"id": 13, "method": "set_vacuum_enabled",
                "params": {"module_id": "VacuumNozzle1", "enabled": True}}))
print(call_rpc({"id": 14, "method": "force_release_vacuum",
                "params": {"module_id": "VacuumNozzle1"}}))

# 15) 处理 AXIS_BLOCKED:收到后按 §6.0 解锁再重发原命令
resp = call_rpc({"id": 15, "method": "apply_command", "params": {
    "module_id": "LinearAxisX", "action": "move_to",
    "payload": {"target_x": 10.0},
}})
if not resp.get("ok") and resp.get("error", {}).get("code") == "AXIS_BLOCKED":
    call_rpc({"id": 16, "method": "enable_collision_detection", "params": {}})
    # 解锁后重发……
```

---

## 9. 已知坑 / 易错点

1. **响应不带 `method` / `module_id` / `action`**，只能靠请求方自维护的 `id` 字段关联。
2. **state_push 是 1:N 广播**，每个 client 有独立的 `interval_s`，但 snapshot 是同一份全量。
3. **`_bridge.call` 默认 timeout 10s**（`rpc/server.py:_MainThreadBridge`），如果 Blender 主线程卡死，apply_command 会超时报 `INTERNAL_ERROR`。
4. **跨线程 client 的"看不见响应"陷阱**：dispatch 走 `bpy.app.timers` 异步调度，如果在主线程正忙（例如 MCP `execute_code` 同步执行中）期间发命令，server 端排队等 timer，client 端可能先 timeout 关闭 socket。生产客户端要么走独立线程长连接收 push、要么按需 sleep ≥1 个 tick（≥ 33 ms）再断。
5. **SimulationManager.apply_command 静默吞 unknown module**（`simulation_manager.py`）；RPC 层在 dispatch 闭包里**先** `manager.get(module_id)` 显式检查，所以 RPC 客户端拿到的是 `AXIS_NOT_FOUND` —— 但如果你直接调 Python API，unknown module 是 no-op。
6. **LinearAxis 单位不对称**：见 §4.6.2 警告。客户端开发者**应当**始终按 BU/s 发 `velocity`，按 BU 发 `target_x`（snapshot 反馈是 mm/mm·s⁻¹），并通过 `scene.unit_settings.scale_length` 自行换算。Conveyor 的 `target_speed` 同样是 BU/s 语义（无 0.001 因子，见 §6.3）。短期最快的 workaround 是用 `axis_ops.send_command` 路径走 mm/s。
7. **本地端口暴露风险**：addon 没有认证。**不要**把 9877 端口通过路由/Docker `-p` 暴露到非 loopback 接口。
8. **RotateAxis 的 `move_to` 用 `duration_s`，LinearAxis 用 `velocity`**：两个 axis 的同名 action 参数语义不同。发起前请查 §4.6.2。
9. **`snapshot` 字段名因 axis 类型而异**：LinearAxis 是 `current_x`，RotateAxis 是 `current_angle`。消费方要按 `kind` 分支处理（或用 `set(keys) & {'current_x', 'current_angle'}`）。
10. **未知 action 静默成功**：`apply_command` 的 action 词表由各模块自己校验，RPC 层不拦 —— 拼错 action（或对错误的模块发 action，如给轴类发 `reset_collision`）会返回 `ok=true` 但**什么都没发生**。发之前查 §4.6.1 / §6 对应模块。
11. **轴类 BLOCKED 只能走 §6.0 的三条路径解除**：给轴类发 `apply_command` 在 BLOCKED 期间一律 `AXIS_BLOCKED`（包括 `reset_collision`），不要试图用 `stop` 先停下来 —— 一样会被拒。
12. **Conveyor 的 `state` 可能滞后于 `running`**（§6.3）：panel 直写 `cfg.running` 不经 `set_running` 时，`update()` 每 tick 只镜像 `running` 不改 `state`。判断启停**以 `running` 为准**，`state` 只用于识别 `blocked`。

---

## 10. 相关源码位置

| 内容 | 路径 |
|------|------|
| RPC server 主体（method 路由 / handlers） | `rpc/server.py` |
| RPC server register/unregister | `rpc/__init__.py` |
| `_MainThreadBridge`（线程 marshalling） | `rpc/server.py` |
| `SimulationCommand` 数据类 | `framework.py` |
| `SimulationManager.apply_command` | `simulation_manager.py` |
| `SimulationManager.snapshot` 含 `collision` 块 | `simulation_manager.py::SimulationManager._collision_snapshot` |
| `AxisBlockedError` → RPC `AXIS_BLOCKED` | `modules/axis_errors.py` |
| `CollisionEngine.set_disabled` / `is_enabled` / `clear_collision` | `modules/components/collision.py` |
| LinearAxis `VALID_ACTIONS` / `apply_command` | `modules/LinearAxix/axis.py` |
| RotateAxis `VALID_ACTIONS` / `apply_command` | `modules/RotateAxis/rotate_axis.py` |
| Conveyor `VALID_ACTIONS` / `start` action / snapshot | `modules/Conveyor/runtime.py` |
| `_handle_set_conveyor_running` handler | `rpc/server.py` |
| Cylinder `VALID_ACTIONS` / `set_outputs` | `modules/Cylinder/cylinder.py` |
| `_handle_set_outputs` handler | `rpc/server.py` |
| VacuumNozzle `VALID_ACTIONS` / `set_enabled` / `force_release` | `modules/VacuumNozzle/runtime.py` |
| `_handle_set_vacuum_enabled` / `_handle_force_release_vacuum` | `rpc/server.py` |
| ApproachSensor `VALID_ACTIONS` / runtime | `modules/ApproachSensor/runtime.py` |
| `aggregate_axis_sensor_states` | `modules/components/sensor/base_sensor.py` |
| `ApproachSensor` 类 + RNA 属性 | `modules/components/approach_sensor/` |
| 离线测试（RPC 面） | `tests/test_rpc_set_outputs.py`、`tests/test_rpc_set_conveyor_running.py`、`tests/test_rpc_collision_enable_disable.py`、`tests/test_linear_axis_rpc_home_unified.py` |
| 离线测试（模块面） | `tests/test_conveyor*.py`、`tests/test_cylinder.py`、`tests/test_vacuum_nozzle*.py`、`tests/test_approach_sensor*.py`、`tests/test_collision_state_snapshot.py` |