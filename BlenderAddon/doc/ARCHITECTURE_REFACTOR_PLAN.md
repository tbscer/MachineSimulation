# LinearAxis 架构重构规划

## 1. 目标

当前项目已经完成了 LinearAxis 的核心功能，但从后续扩展角度看，现有结构仍然偏向“单一线性轴模拟器”。

下一阶段应把它演进为一个“统一模拟引擎 + 多种运动模块 + 可扩展组件”的架构，以便后续接入：

- LinearAxis：线性运动模块
- RotateAxis：旋转运动模块（后续新增）
- 其他可扩展模拟组件

---

## 2. 总体思路

将系统拆分为四层：

1. SimulationManager 层
   - 统一调度所有模块的 tick
   - 统一管理注册、注销、状态快照、命令分发

2. Module Runtime 层
   - 每种运动模块独立实现自己的逻辑
   - 例如 LinearAxisRuntime、RotateAxisRuntime

3. Component 层
   - 负责具体的子部件逻辑
   - 例如 Rail、Slider、Sensor、Trigger、Limiter、Constraint 等

4. Adapter / Discovery 层
   - 负责从 Blender 场景对象中发现并绑定模块
   - 负责把场景对象映射为运行时对象

---

## 3. 期望的系统结构

```text
SimulationManager
 ├── Module Registry
 │    ├── LinearAxisRuntime
 │    ├── RotateAxisRuntime
 │    └── Other Runtime Modules
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

## 4. 各层职责

### 4.1 SimulationManager

SimulationManager 负责“总控”，但不直接承担具体运动算法。

它应负责：

- 启动/停止模拟循环
- 注册/注销模块
- 每 tick 调用所有模块的 update(dt)
- 统一处理命令分发
- 收集各模块状态并提供快照
- 处理模块异常，避免单模块出错影响整个系统

### 4.2 Module Runtime

每个模块都应提供统一接口，例如：

- update(dt)
- apply_command(cmd)
- reset()
- snapshot()

这样 Manager 就可以对所有模块一视同仁，而不需要为每种模块单独写分支逻辑。

### 4.3 Component

Component 层负责“物理/逻辑细节”。

例如：

- SliderComponent：负责位移和速度
- RailComponent：负责边界与限制
- SensorComponent：负责检测触发状态
- TriggerShimComponent：提供干净的触发源
- 后续可扩展的 ConstraintComponent、LogicComponent 等

### 4.4 Discovery / Adapter

这个层主要负责把 Blender 场景里的对象接入运行时系统。

职责包括：

- 发现 host 对象
- 根据命名规则或配置绑定子对象
- 根据模块类型创建对应 Runtime
- 把 Blender 对象映射为运行时组件

---

## 5. 对当前项目的映射建议

### 5.1 simulation_manager.py

当前的 SimulationManager 已经具备“tick 调度”的基本能力，建议升级为：

- 从“管理 axes”升级为“管理所有 simulation modules”
- 维持现有的 tick 机制，但接口更抽象

### 5.2 axis.py

当前的 axis.py 更像是“单个 LinearAxis 的具体运行时实现”。
建议将它从“具体实现”升级为“模块实现的一种”。

后续建议拆分为：

- BaseMotionModule（抽象基类）
- LinearAxisRuntime（当前功能）
- RotateAxisRuntime（后续新增）

### 5.3 discovery.py

当前 discovery.py 主要负责发现 LinearAxis host 并构造 LinearAxis。
建议提升为：

- 根据 host 类型/命名规则发现不同模块
- 自动创建对应 runtime
- 支持未来新增 RotateAxis / 其他模块

### 5.4 component.py

当前 component.py 中的 LinearAxisProperty 仍然是“线性模块专用配置”。
建议拓展为：

- 基础模块配置
- LinearAxis 配置
- RotateAxis 配置
- 其他组件配置

也就是说，属性组不再只服务一个模块，而是服务整个模拟框架。

---

## 6. 推荐的抽象接口

建议为所有模块定义统一接口，例如：

```python
class SimulationModule:
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

这样 Manager 逻辑可以保持稳定，对后续新增模块友好。

---

## 7. 推荐的后续实施顺序

### 第一阶段：先抽象管理层

- 把 SimulationManager 从“轴管理器”升级为“模块管理器”
- 保证现有 LinearAxis 功能不受影响

### 第二阶段：抽象模块接口

- 定义统一模块接口
- 让 LinearAxisRuntime 实现这个接口

### 第三阶段：预留 RotateAxis 接口

- 先定义 RotateAxisRuntime 的接口和配置入口
- 但暂时不实现具体逻辑

### 第四阶段：扩展组件层

- 把 sensor、trigger、limiter 等组件抽象成可插拔对象
- 为后续其他模块提供复用能力

### 第五阶段：升级发现逻辑

- 从“发现 LinearAxis”升级为“发现任意模块”
- 支持按类型动态创建不同 runtime

---

## 8. 设计原则

这一版重构的关键不是“把现有代码改得更复杂”，而是让架构具备以下特征：

- 可扩展：能轻松添加 RotateAxis
- 可插拔：组件可以独立替换
- 可维护：每一层职责清晰
- 可复用：相同组件可以服务不同模块
- 不破坏现有功能：先不动现有逻辑，先重构结构

---

## 9. 结论

你当前项目已经具备一个很好的“模拟核心雏形”，但更适合演进为：

- 统一 simulation manager
- 多种运动模块 runtime
- 可扩展组件系统

这套方向能够为你后续接入 RotateAxis 和更多功能件提供稳定基础。