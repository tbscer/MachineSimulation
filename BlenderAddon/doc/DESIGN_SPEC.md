# LinearAxis / RotateAxis / Simulation Framework 设计规范

## 1. 目标

将当前 Blender addon 从“面向 LinearAxis 的单一运动模拟器”演进为一个“统一模拟框架”，支持：

- LinearAxis：线性运动模块
- RotateAxis：旋转运动模块
- 未来更多模拟组件：传感器、触发器、逻辑单元、约束器等

目标是建立一套可扩展、可维护、可插拔的模拟引擎骨架，确保后续新增模块时不需要重构整个系统。

---

## 2. 总体设计原则

### 2.1 统一调度

所有模块通过统一的 SimulationManager 进行调度，而不是让 Manager 直接硬编码某一种模块逻辑。

### 2.2 模块化

每一种运动模块都实现统一接口：

- update(dt)
- apply_command(cmd)
- reset()
- snapshot()

### 2.3 组件可插拔

模块由多个组件组成，这些组件可以独立扩展：

- 运动组件
- 传感器组件
- 限位组件
- 触发组件
- 逻辑组件

### 2.4 Blender 兼容

该框架仍然是 Blender addon，因此仍然基于：

- bpy.types.PropertyGroup
- bpy.app.timers
- Blender Object / Empty / Mesh 结构

### 2.5 兼容现有逻辑

当前已经实现的 LinearAxis 功能应继续保留，并尽量保持现有调用方式可用，避免一次性破坏所有行为。

---

## 3. 系统层次结构

```text
SimulationManager
 ├── Module Registry
 │    ├── LinearAxisRuntime
 │    ├── RotateAxisRuntime
 │    └── Future Modules
 ├── Tick Loop
 ├── Command Bus
 └── State Snapshot

LinearAxisRuntime
 ├── SliderComponent
 ├── RailComponent
 ├── SensorComponent
 ├── TriggerShimComponent
 └── Motion Logic

RotateAxisRuntime
 ├── RotatorComponent
 ├── LimitComponent
 ├── SensorComponent
 └── Motion Logic
```

---

## 4. 核心抽象

### 4.1 BaseSimulationModule

所有模块运行时都应继承统一基类：

```python
class BaseSimulationModule:
    module_id: str
    kind: str

    def update(self, dt: float) -> None:
        ...

    def apply_command(self, cmd) -> None:
        ...

    def reset(self) -> None:
        ...

    def snapshot(self) -> dict:
        ...
```

### 4.2 SimulationCommand

所有模块命令都通过统一命令对象传递：

```python
class SimulationCommand:
    action: str
    payload: dict
```

### 4.3 SimulationManager

SimulationManager 是统一调度中心。

职责：

- 注册模块
- 注销模块
- 调用每个模块的 update(dt)
- 分发命令
- 收集状态快照
- 管理 tick 生命周期

---

## 5. 模块设计

### 5.1 LinearAxisRuntime

负责线性模块的运动逻辑，包含：

- move_to(target_x, duration_s)
- set_velocity(v)
- home(direction, velocity)
- stop / idle
- state machine

现阶段它仍然保留当前已有逻辑，作为第一个正式模块实现。

### 5.2 RotateAxisRuntime

负责旋转模块的运动逻辑，后续补齐。

目前作为占位骨架，接口已定义，后续可扩展为：

- rotate_to(angle, duration_s)
- set_angular_velocity(v)
- stop()
- reset()

### 5.3 Future Modules

将来可扩展为：

- ConveyorAxis
- GripperModule
- TriggerLogicModule
- SensorGroupModule

---

## 6. 配置设计

### 6.1 PropertyGroup 设计目标

当前 [component.py](component.py) 中的 `LinearAxisProperty` 应演进为“通用模块配置”，而不仅仅是线性轴配置。

### 6.2 基础字段

所有模块共享基础字段：

- `enabled`
- `module_kind`

### 6.3 LinearAxis 字段

保留原有字段：

- `rail`
- `slider`
- `trigger_shim`
- `home_sensor`
- `pos_sensor`
- `neg_sensor`
- `stroke`
- `speed`
- `home_speed`

### 6.4 RotateAxis 预留字段

后续扩展字段：

- `rotator`
- `rotate_home_sensor`
- `rotate_limit_sensor`
- `angle`
- `angular_speed`

---

## 7. 发现与注册设计

### 7.1 Discovery 目标

Discovery 负责从 Blender 场景对象中发现模块，并将其注册到 SimulationManager。

### 7.2 发现规则

- Empty 名称以 `LinearAxis` 开头时，创建 LinearAxisRuntime
- Empty 名称以 `RotateAxis` 开头时，创建 RotateAxisRuntime
- 后续可根据 `module_kind` 配置进一步扩展

### 7.3 注册方式

发现后通过：

- `manager.register_module(module)`

统一注册。

---

## 8. 兼容层设计

为了避免一次性破坏当前 addon 的运行方式，保留以下兼容层：

- `register()` / `unregister()` 仍然是 Blender addon 主入口
- `SimulationManager.register()` 仍然保留，作为旧接口兼容
- 现有 `axis_ops` / `send_command` / `read_state` 调用路径继续可用

---

## 9. 运行流程

### 9.1 Addon 启动

1. 注册 PropertyGroup
2. 创建 SimulationManager
3. 启动 tick loop
4. 执行 discovery
5. 将发现到的模块全部注册

### 9.2 每 Tick

1. SimulationManager 依次调用各模块的 `update(dt)`
2. 模块内部更新自己的状态
3. 模块状态写回到 Blender 对象或自定义属性

### 9.3 外部命令

外部可以通过：

- Python API
- 自定义属性 bridge
- 未来的 MCP / C# bridge

把命令发给 SimulationManager，最终调度到具体模块。

---

## 10. 后续实施顺序

### Phase 1：框架骨架

- 建立 `BaseSimulationModule`
- 建立 `SimulationCommand`
- 建立 `SimulationManager`
- 建立 `LinearAxis` 模块接口接入

### Phase 2：模块扩展

- 加入 `RotateAxisRuntime` 占位骨架
- 建立模块工厂
- 建立 discovery 与注册入口

### Phase 3：配置扩展

- 扩展 PropertyGroup 为通用配置
- 为 RotateAxis 预留字段
- 让 UI 后续可按模块类型显示配置项

### Phase 4：组件化

- 抽象传感器、触发器、极限组件
- 让多个模块共享组件逻辑

### Phase 5：真实功能接入

- 完成 RotateAxis 的物理/运动逻辑
- 接入更多模块类型
- 逐步把旧接口迁移为统一接口

---

## 11. 设计结论

这套设计目标不是“把 LinearAxis 继续堆功能”，而是把它演化为一个真正的运动模拟框架。

最终会形成：

- SimulationManager：统一调度中心
- LinearAxisRuntime：当前已完成的线性模块
- RotateAxisRuntime：后续扩展的旋转模块
- 可扩展组件与配置系统：支撑未来更多功能件