# LinearAxis / RotateAxis 项目工程索引

## 1. 项目定位

本项目是一个基于 Blender 的运动模拟 addon，当前已具备 LinearAxis 的完整运行骨架，并正在演进为一个可扩展的多模块模拟框架。

---

## 2. 核心文档

- [DESIGN_SPEC.md](DESIGN_SPEC.md)  
  总体设计规范，说明系统目标、层次结构、接口抽象与扩展方向。

- [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)  
  系统设计说明书，面向整体架构理解和后续开发。

- [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md)  
  实施路线图，说明如何逐步推进到多模块框架。

---

## 3. 核心代码

- [framework.py](framework.py)  
  定义统一模块接口与命令对象。

- [simulation_manager.py](simulation_manager.py)  
  统一调度器，管理模块生命周期与 tick。

- [axis.py](axis.py)  
  LinearAxis 运行时，当前已接入模块接口。

- [rotate_axis.py](rotate_axis.py)  
  RotateAxis 占位骨架，供后续实现。

- [modules.py](modules.py)  
  模块工厂，用于根据对象类型创建不同模块。

- [component.py](component.py)  
  Blender PropertyGroup 配置层，当前已扩展为通用模块配置。

- [discovery.py](discovery.py)  
  场景发现与注册逻辑。

- [addon.py](addon.py)  
  Blender addon 注册与生命周期入口。

---

## 4. 测试与验证

- [tests/test_framework.py](tests/test_framework.py)  
  验证模块接口与 SimulationManager 的基础框架。

- [tests/test_discovery.py](tests/test_discovery.py)  
  验证 discovery 和 LinearAxis 发现逻辑。

---

## 5. 后续开发建议

### 优先级 1：完善模块发现
- 将 discovery 从 LinearAxis 单分支扩展为多模块分支。

### 优先级 2：完善 RotateAxis 骨架
- 给 RotateAxis 补齐完整的命令接口与状态字段。

### 优先级 3：抽象组件层
- 将 Sensor / Trigger / Limit 抽象为可共享组件。

### 优先级 4：完善 UI 配置
- 根据 module_kind 显示不同配置项。

---

## 6. 开发原则

- 保持现有 LinearAxis 功能稳定。
- 新功能优先通过模块接口接入。
- 任何新增模块都应能被 SimulationManager 调度。
- 配置和发现逻辑应支持未来扩展。