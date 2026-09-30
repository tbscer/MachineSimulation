# LinearAxis / RotateAxis 实施路线图

## 目标

把当前 addon 从“线性轴模拟器”演进为“多模块运动模拟框架”。

---

## 第一阶段：框架骨架

### 目标

建立统一的模块接口和调度器。

### 事项

- 新增 `BaseSimulationModule`
- 新增 `SimulationCommand`
- 新增 `SimulationManager` 模块调度能力
- 让 `LinearAxis` 实现统一模块接口

### 交付结果

- Manager 可以调度多个模块
- LinearAxis 仍然可以正常工作
- 后续模块可以平滑接入

---

## 第二阶段：模块扩展

### 目标

为后续 RotateAxis 和其他模块预留骨架。

### 事项

- 新增 `RotateAxisRuntime` 占位类
- 新增模块工厂 `build_module()`
- discovery 能识别不同模块类型

### 交付结果

- 发现逻辑不再只面向 LinearAxis
- 新模块可以通过统一入口注册

---

## 第三阶段：配置系统扩展

### 目标

让配置层支持多种模块类型。

### 事项

- 扩展 `LinearAxisProperty` 为通用模块配置
- 增加 `module_kind`
- 为 RotateAxis 预留配置字段

### 交付结果

- 配置层可用于 LinearAxis / RotateAxis / 后续模块
- UI 可以按模块类型显示不同配置项

---

## 第四阶段：组件化

### 目标

把运动控制与子组件拆分开，提升复用性。

### 事项

- 抽象 SensorComponent
- 抽象 TriggerComponent
- 抽象 LimitComponent
- 让多个模块共享组件逻辑

### 交付结果

- 模块逻辑更清晰
- 后续新增功能件更容易

---

## 第五阶段：业务功能完善

### 目标

完成 RotateAxis 的真实功能，并为更多模块准备升级空间。

### 事项

- 实现 RotateAxis 的运动逻辑
- 连接到 SimulationManager
- 支持更多命令与状态字段

### 交付结果

- 系统真正从“LinearAxis 单模块”演进为“多模块模拟框架”