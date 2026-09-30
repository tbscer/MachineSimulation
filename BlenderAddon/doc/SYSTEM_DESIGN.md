# LinearAxis / RotateAxis 模拟框架系统设计说明书

## 1. 系统定位

本项目是一个基于 Blender 的模拟控制框架，目标不是单纯实现 LinearAxis，而是构建一个可扩展的运动模拟引擎。

当前阶段已经具备：
- LinearAxis 的运动控制
- SimulationManager 的 tick 调度
- 基础组件（slider / rail / sensor / shim）

后续目标：
- 接入 RotateAxis
- 支持更多模拟模块与组件
- 保持插件结构清晰、可扩展、可维护

---

## 2. 设计目标

### 2.1 功能目标

- 统一管理所有模拟模块
- 支持 LinearAxis 与 RotateAxis 共存
- 未来可以扩展更多模块
- 保持现有 LinearAxis 功能可用

### 2.2 架构目标

- 模块化
- 可扩展
- 可插拔
- Blender 原生兼容
- 易于测试

---

## 3. 总体架构

### 3.1 分层结构

```text
Blender Scene
  └── Addon Entry
        └── SimulationManager
              ├── LinearAxisRuntime
              ├── RotateAxisRuntime
              └── Future Modules
```

### 3.2 责任划分

- SimulationManager：统一调度与状态管理
- LinearAxisRuntime：负责线性模块的运动逻辑
- RotateAxisRuntime：负责旋转模块的运动逻辑占位骨架
- Component：提供传感器、轨道、滑块、触发器等基础能力
- Discovery：负责从 Blender 场景发现对象并绑定模块

---

## 4. 核心抽象

### 4.1 BaseSimulationModule

所有模拟模块都实现统一接口：

- update(dt)
- apply_command(cmd)
- reset()
- snapshot()

### 4.2 SimulationCommand

统一命令对象：

- action
- payload

### 4.3 SimulationManager

SimulationManager 是整个框架的核心调度器：

- 注册/注销模块
- 每 tick 更新所有模块
- 分发命令
- 汇总状态快照

---

## 5. 当前代码结构映射

### 5.1 现有模块

- [simulation_manager.py](simulation_manager.py)
  - 模拟调度器
- [axis.py](axis.py)
  - LinearAxis 运行时
- [component.py](component.py)
  - Blender 属性配置层
- [discovery.py](discovery.py)
  - 场景对象发现与注册
- [core/](core/)
  - Slider / Rail / Sensor / Trigger 组件

### 5.2 新增框架层

- [framework.py](framework.py)
  - 统一模块与命令抽象
- [rotate_axis.py](rotate_axis.py)
  - RotateAxis 占位模块
- [modules.py](modules.py)
  - 模块工厂

---

## 6. Blender 集成方式

### 6.1 属性组

当前使用 `bpy.types.PropertyGroup` 将配置挂载到 Blender 对象上。

未来配置层将支持：
- 通用模块字段
- LinearAxis 专属字段
- RotateAxis 预留字段

### 6.2 定时器

当前使用 Blender 的 `bpy.app.timers` 来驱动模拟循环，确保所有 bpy 操作都发生在主线程上。

### 6.3 对象发现

Discovery 会扫描场景中的 Empty 对象，并根据命名规则与配置决定创建哪一种模块。

---

## 7. 未来扩展能力

### 7.1 RotateAxis

未来可扩展为：
- rotate_to(angle, duration_s)
- set_angular_velocity(v)
- stop()
- reset()

### 7.2 其他模块

未来可在同一框架下继续接入：
- Gripper
- Conveyor
- LogicTrigger
- SensorGroup

---

## 8. 设计演进路线

### Phase 1：框架骨架完成
- 模块接口
- 调度器
- 兼容型 LinearAxis 接入

### Phase 2：多模块准备
- RotateAxis 占位骨架
- 模块工厂
- discovery 分支化

### Phase 3：组件化
- 把传感器、限制、触发器抽象成可复用组件

### Phase 4：真正功能接入
- 完成 RotateAxis 的逻辑
- 支持更多模块与命令类型

---

## 9. 结论

当前项目已经不再只是一个“LinearAxis addon”，而是向一个“可扩展运动模拟框架”演进。

后续开发应该以“模块化 + 通用接口 + 可扩展配置”为核心原则，确保系统能够持续增长，而不是不断在单一功能上堆积代码。