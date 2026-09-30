# Cylinder 综合对象

> 把 `cylinder` 模块的架构与用法集中记在这里 —— 适用于"线性气缸 / 推杆"
> 类的双工位应用:由两个 `approach sensor` 定义两个工作位置,外部信号
> `Output_1` / `Output_2` 决定目标位置,cylinder 自主驱动 work bar 沿
> 轴线运动,触到对应 sensor 的虚拟立方体就停下。

## 1. 组件结构

```
Cylinder (EMPTY host)                  ←── host, 命名 Cylinder*
├─ WorkBar (MESH, 真圆柱体)            ←── 被 cylinder 每 tick 驱动的运动体
├─ TouchShime (MESH)                   ←── 检测片,跟随 WorkBar 一起移动
├─ ApproachSensor.Work1 (MESH)         ←── 工作位置 1 的 approach sensor
└─ ApproachSensor.Work2 (MESH)         ←── 工作位置 2 的 approach sensor
```

注意:**没有 rail** —— cylinder 不包装 LinearAxis,运动方向由两 approach
sensor 的世界位置差推出(`axis_dir`),运动边界由 approach sensor 触发
状态决定。

## 2. ApproachSensor —— 独立组件

不同于既有的 `UTypeSensor`(用 sensor mesh 自身 AABB 切薄片),
`ApproachSensor` 用一个**中心对齐的轴对齐盒 (axis-aligned box)**
检测 trigger 是否进入触发区。盒的三维独立, 不再需要 normal_axis
方向语义。

### 2.1 盒几何(传感器局部坐标系)

- **中心偏移**: `working_face_center` (Vector3, 默认 `(0, 0, 0)`)
  —— 盒中心在 sensor 局部系的位置
- **尺寸**: `cube_size` (Vector3, 默认 `(1, 1, 1)`)
  —— 盒在三个轴上的全长 (l, w, h), 半边长分别为 `cube_size[i] / 2`

盒在 sensor 局部系下:
- `cube_min[i] = working_face_center[i] - cube_size[i] / 2`
- `cube_max[i] = working_face_center[i] + cube_size[i] / 2`

默认配置: 盒与 sensor mesh 同心, 各方向 ±0.5 的 1×1×1 立方体。

### 2.2 RNA 属性(挂在 sensor mesh 上)

| 属性 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `approach_sensor_cube_size` | FloatVector(3) | (1, 1, 1) | 盒尺寸 (l, w, h), 中心对齐 |
| `approach_sensor_working_face_center` | FloatVector(3) | (0, 0, 0) | sensor 局部坐标系下的盒**中心偏移** |
| `approach_sensor_trigger` | Pointer(Object) | (空) | 检测对象(一般 = cylinder 的 TouchShime) |

`obj["sensor_type"] = "approach"` —— 由 `ApproachSensor.__init__`
在构造时写入,供 overlay 识别。

**已删除**: 旧版有 `approach_sensor_normal_axis` (Enum X/Y/Z) 字段,
让盒从 `working_face_center` 沿 +normal 方向延伸。该设计引入了
"方向"概念, 但与"中心 + 长宽高"描述重复, 容易让 artist 误配导致
盒陷进物体内部。新版去除 normal_axis, 盒中心对齐, 三个维度独立
设置, 描述更直观。

### 2.3 检测算法

把 `trigger_obj` 的 8 个世界 AABB 角点变换到 sensor 局部系 → OBB;
盒在 sensor 局部系里是 AABB。SAT 判定 15 个分离轴(3 个 trigger
OBB 轴 + 3 个盒 AABB 轴 + 9 个叉积),任一轴出现分离 → 不触发。

### 2.4 视口可视化

Approach sensor overlay 与既有 sensor overlay 共用 scene 开关
`bpy.types.Scene.ms_示`:
- 盒 12 条棱线框:未触发 = 琥珀 `#FFA500`,触发 = 红 `#FF3030`
- 6 面半透明填充(同色 alpha 0.18)

## 3. CylinderModule —— 完全独立的运动

### 3.1 状态机

```
IDLE ──outputs (T,F)|(F,T) & current != target──> MOVING_TO_<target>
MOVING_TO_<target> ──target sensor.is_triggered──> IDLE
* ──自己命中，或别的 module 命中（scene marker）──> BLOCKED
BLOCKED ──reset_collision（本模块或别人的）──> IDLE
```

collision 语义与 `LinearAxis` / `RotateAxisRuntime` 完全共用（同一个
`CollisionEngine` 单例、同一个 scene marker）：

- 命中且引擎启用 → `BLOCKED` + `stop_reason="collision"` +
  `engine.mark_collision()` 写 `scene["motion_simulation_collision"]`；
  **其它** module 下一 tick 读到该 marker 也会被锁住（全局停车）。
- BLOCKED 期间 `apply_command()` 只放行 `reset_collision` 与 `set_outputs`
  （后者只是外部控制信号位，不产生运动）；`stop` / `idle` 会被丢弃。
- 别人清了 marker（`reset_collision` / Disable Collision）→ 本模块下一 tick
  自行退出 BLOCKED。
- 引擎禁用（`is_enabled == False`）时仍会跑 BVH 检查（供 Dev panel 显示
  `last_collision`），但**不会**重新锁进 BLOCKED。

### 3.2 outputs 映射

| `output_1` | `output_2` | 目标 |
|------------|------------|------|
| T | F | 1 |
| F | T | 2 |
| T | T | None(非法,保持 IDLE) |
| F | F | None(非法,保持 IDLE) |

### 3.3 当前位置

由 approach sensor 触发状态决定:`sensor_1.is_triggered` → 1,`sensor_2.is_triggered`
→ 2,否则 None。两 sensor 同时触发时 1 优先。

### 3.4 每 tick 决策

顺序与 `LinearAxis` / `RotateAxisRuntime` 保持一致：

1. 引用存活检查（stale 引用 → `_alive = False`）
2. 镜像 cfg `output_1` / `output_2` 到 instance(让 RPC `set_outputs` 与
   panel toggle 都生效)
3. 首次记录 work_bar 基准位置（给 `snapshot.current_x` 用）
4. **全局 collision 门控**：scene marker 已清且自己 BLOCKED →
   `_clear_blocked_state_only()` 自动解锁；marker 置位 →
   `_enter_blocked_state()` 并放弃本 tick
5. 跑两 `ApproachSensor.update()`
6. 决定 target / current
7. 非法 outputs → IDLE
8. current == target → IDLE
9. 否则 → state = MOVING_TO_<target>;`velocity_signed = direction_sign * target_speed`
10. 积分:`work_bar.location += axis_dir * velocity_signed * dt`
11. 同步 `touch_shim`（若 `touch_shim.parent == work_bar`，parent-child 已保证
    跟随，跳过手动写入）
12. 再次 sensor update,target sensor 触发则 IDLE
13. collision engine 检查:`rebuild_if_due(depsgraph, axis_id=module_id)` 刷新障碍
    缓存 → `check_slider_collision(work_bar, depsgraph, axis_id=module_id)`
    （**引擎禁用时也调**，保证 Dev panel 的 `last_collision` 最新）→
    命中且 `engine.is_enabled` → `_enter_blocked_state()`（内部调
    `mark_collision()` 写 scene marker）

### 3.5 touch_shim 同步(关键设计点)

**不要求** `touch_shim.parent == work_bar`。cylinder 每 tick 把 work_bar
的位移同步施加到 touch_shim 上,让两者保持构造时记录的初始 world 偏移。
这样在 scene 里它们都是 host 的子物体、互相独立,cylinder 用运行时同步
保证协同。

## 4. RPC 集成

### 4.1 `set_outputs` 方法

```json
{
  "id": <id>,
  "method": "set_outputs",
  "params": {
    "module_id": "Cylinder1",
    "output_1": true,
    "output_2": false
  }
}
```

**响应**:
```json
{
  "ok": true,
  "id": <id>,
  "result": {"output_1": true, "output_2": false}
}
```

**错误码**:
- `INVALID_PARAMS`:`module_id` 非字符串,或 `output_1` / `output_2` 不是 bool
- `AXIS_NOT_FOUND`:`module_id` 不在 manager 里,或 module 不支持 `set_outputs`

`output_1` / `output_2` 写入 module instance + cfg;下一个 tick 自然
reconcile 到 IDLE / MOVING_TO_X。

## 5. snapshot(state_push)

每个 Cylinder 的 snapshot:

| 字段 | 类型 | 说明 |
|------|------|------|
| `module_id` | string | Cylinder host 名(同 `name`) |
| `kind` | string | 固定 `"cylinder"` |
| `name` | string | 同 `module_id` |
| `state` | string | runtime state: `"idle"` / `"moving_to_1"` / `"moving_to_2"` / `"blocked"` |
| `current_state` | string | 位置状态: `"approach_sensor_1"` / `"approach_sensor_2"` / `"unknown"`(两 sensor 均未触发) |
| `approach_sensor_1` | bool | `sensor_1.is_triggered` 的镜像 |
| `approach_sensor_2` | bool | `sensor_2.is_triggered` 的镜像 |
| `output_1` | bool | 输出信号 1 |
| `output_2` | bool | 输出信号 2 |
| `stop_reason` | string | 停止原因 |

设计要点:
- `state`、`current_state`、`approach_sensor_1/2` 三个独立维度:
  `state` 反映 runtime 行为(在动还是停了);`current_state` 是聚合后的
  位置状态字符串;`approach_sensor_1/2` 是底层 sensor 的原始 bool。
  `state == "idle"` 时 `current_state` 仍然可以是 `"unknown"`(停在两个
  sensor 之间)或 `"approach_sensor_1"`(停在 sensor 1 位置)。
  `state == "moving_to_1"` 中途可能 `current_state == "unknown"`(还没到),
  接近到达时 `current_state == "approach_sensor_1"`。两 sensor 同时触发时
  `current_state` 固定为 `"approach_sensor_1"`(优先级 1 > 2),
  但 `approach_sensor_1` / `approach_sensor_2` 两个 bool 都为 True。
- **不再**下发 `current_x`(BU 位移) / `velocity`(BU/s 有符号速度):
  这两个量只是 runtime 的中间状态,与 `state` + `current_state` 强相关。
  客户端能且只需从 `state` + `current_state` + 两 sensor bool 推出当前阶段。
- 也不再下发 `current_position` / `target_position`(int 1/2/None):
  被合并到 `current_state` 字符串。

`SimulationManager.snapshot()` 的 `cylinders` 列表里 cylinder 独立成桶
(与 LinearAxis / RotateAxisRuntime 的 `axes` 桶并列)—— 见 `doc/RPC.md` §5。

## 6. 配置面板

`OBJECT_PT_cylinder` (`PROPERTIES` > `object`,选中物体是 `Cylinder*` host 时显示):

- **Host refs**: work_bar / touch_shim / approach_sensor_1 / approach_sensor_2
- **Motion parameters**: target_speed (mm/s)
- **Output signals**: output_1 / output_2 toggle(写入即生效,无需 Apply)
- **Approach sensors**: 两个 approach sensor 的
  cube_size / working_face_center(从 host 直接访问 sensor 的 RNA)
- **Runtime state**: state / current_state / approach_sensor_1 /
  approach_sensor_2 / stop_reason

`Refresh` 按钮触发重发现;`Apply Outputs` 按钮显式把 panel 上的
output_1/output_2 推到 instance。

## 7. 测试覆盖

- `tests/test_approach_sensor.py` —— 18 个用例覆盖几何 / 触发逻辑 / 状态翻转
- `tests/test_cylinder.py` —— 20 个用例覆盖构造 / outputs 决策 / 运动积分 /
  touch_shim 同步 / outputs 反转 / command 接口 / snapshot schema
- `tests/test_rpc_set_outputs.py` —— 7 个用例覆盖 RPC 路由 / 参数校验 /
  错误码

总计 45 个离线测试,无需 Blender 即可验证。

## 8. 已知限制

- **Work bar 父级旋转**:当前实现假设 host 是 identity 旋转。如果
  `Cylinder` host 带非平凡 rotation,`work_bar.location` 与 world 速度
  之间需要矩阵转换。默认 scene 把 host 放在无旋转位置;如果艺术家后期
  加旋转,需要重新审视这段。
- **两 sensor 同时触发**:优先级取 1。`UI` 里建议提示 artist 两个 sensor
  的虚拟立方体不要重叠。
- **collision engine 在 cylinder 下的语义**:`work_bar` / `touch_shim` /
  `approach_sensor_1` / `approach_sensor_2` **全部**在
  `SimulationManager._AXIS_OBJECT_FIELDS` 里，因此都在 cylinder 自己的
  per-axis 排除集内。

  这不是可选项:`work_bar` 是运动体，而 `touch_shim` 按设计就贴在
  `work_bar` 上、两个 approach sensor 就在工作位置——如果它们被当成
  “非轴障碍物”各自建 BVH，cylinder 一启动就会撞上自己，而且永远解不开。
  相关回归测试:`tests/test_simulation_manager.py::
  test_collect_axis_object_names_covers_cylinder_slots`。