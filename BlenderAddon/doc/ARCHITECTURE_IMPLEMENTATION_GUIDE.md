# LinearAxis 架构改造实施指南

## 1. 改造目标

把当前项目从“面向 LinearAxis 的单一运动模拟器”升级为“统一模拟引擎 + 多模块运行时 + 可扩展组件”的框架。

目标不是一次性全部替换，而是尽量保持现有功能不受影响，在原有基础上逐步演进。

---

## 2. 设计原则

### 2.1 先不破坏现有接口

现有外部调用方式（例如通过 ops 接口或属性桥驱动）应先继续兼容。

也就是说：

- 旧的 LinearAxis 逻辑仍然可用
- 新架构只是在内部做抽象和分层
- 旧调用方式逐渐迁移为统一的模块接口

### 2.2 Manager 只做调度

SimulationManager 不应直接包含“线性运动特定算法”。

它只应该做：

- 注册模块
- 分发命令
- 调用 update(dt)
- 汇总状态

### 2.3 模块与组件解耦

每个模块由“运行时 + 组件集”构成。

例如：

- LinearAxisRuntime 由 Slider / Rail / Sensor / Shim 组成
- RotateAxisRuntime 由 Rotator / Limit / Sensor 组成

### 2.4 支持后续扩展

后续新增模块时，不需要改动整个调度器逻辑，只需要新增一个新的模块实现和注册入口。

---

## 3. 建议引入的核心抽象

### 3.1 BaseModule

建议先定义一个统一基类：

```python
class BaseModule:
    module_id: str
    kind: str

    def update(self, dt: float) -> None:
        raise NotImplementedError

    def apply_command(self, cmd) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError
```

### 3.2 Command 抽象

命令可以统一成一个轻量对象，后续方便扩展：

```python
class ModuleCommand:
    action: str
    payload: dict
```

这样后续无论是 LinearAxis 的 move_to、home、stop，还是 RotateAxis 的 rotate_to、set_velocity，都能走统一命令通道。

### 3.3 ModuleState

状态建议统一成字典或轻量对象：

```python
class ModuleState:
    state: str
    position: float | None = None
    velocity: float | None = None
    sensors: dict | None = None
```

---

## 4. 建议的类结构

### 4.1 SimulationManager

现有 [simulation_manager.py](simulation_manager.py) 可以演进为：

```python
class SimulationManager:
    def __init__(self):
        self.modules = {}
        self._tick_handle = None

    def register(self, module):
        ...

    def unregister(self, module_id):
        ...

    def update(self, dt):
        for module in self.modules.values():
            module.update(dt)

    def apply_command(self, module_id, cmd):
        ...

    def snapshot(self):
        ...
```

### 4.2 LinearAxisRuntime

原来的 [axis.py](axis.py) 可以演进为 LinearAxisRuntime。

它仍然保留现有核心逻辑：

- move_to
- set_velocity
- home
- stop
- update

但只作为一个模块实现，而不是整个系统的唯一核心对象。

### 4.3 RotateAxisRuntime

后续新增的旋转模块建议遵循相同接口：

```python
class RotateAxisRuntime(BaseModule):
    def update(self, dt):
        ...

    def apply_command(self, cmd):
        ...
```

### 4.4 ComponentRegistry

建议引入一个组件注册器，让不同模块复用相同组件类型：

```python
class ComponentRegistry:
    def register(self, name, component_cls):
        ...
```

这一步可以后续再做，但最好提前预留。

---

## 5. 文件级改造建议

### 5.1 [simulation_manager.py](simulation_manager.py)

当前职责已经比较接近调度器。建议改为：

- 不再只管理 axis
- 改为管理 “module registry”
- 保留 tick loop 逻辑
- 增加 `register_module`、`unregister_module`、`apply_command`、`snapshot`

### 5.2 [axis.py](axis.py)

建议做以下调整：

- 保留现有运动算法
- 让它继承 `BaseModule`
- 将原来的 `LinearAxis` 重命名为 `LinearAxisRuntime` 或保持兼容别名

兼容策略：

```python
class LinearAxis(BaseModule):
    ...
```

这样现有代码可继续使用 `LinearAxis`，但内部已经是模块实现。

### 5.3 [discovery.py](discovery.py)

建议把它从“发现 LinearAxis”扩展为“发现模块”。

未来可增加逻辑：

- 识别 host 类型
- 根据配置判断是 LinearAxis 还是 RotateAxis
- 调用不同的构造函数

### 5.4 [component.py](component.py)

建议把当前的 `LinearAxisProperty` 扩展为一个更通用的配置容器：

- `module_kind`
- `module_enabled`
- `module_type`
- `linear_config`
- `rotate_config`

这样一个 object 可以挂载多个配置视图，而不是只绑定一个线性轴配置。

---

## 6. 推荐的演进路径

### 第一步：不改功能，只改结构

- 保留现有 LinearAxis 行为
- 新增 `BaseModule` 抽象
- 让 `LinearAxis` 实现统一接口
- Manager 仍然先兼容当前调用方式

### 第二步：引入模块注册机制

- Manager 增加 `register_module()`
- 不再把所有逻辑写死在 `SimulationManager._tick()` 内

### 第三步：为 RotateAxis 预留入口

- 先定义 `RotateAxisRuntime` 的接口和配置字段
- 但暂时不实现真正的运动逻辑

### 第四步：抽象组件层

- 把 sensors、trigger、limiter 等提炼为可复用组件
- 使不同模块可共享组件逻辑

### 第五步：升级发现逻辑

- 从发现单一 LinearAxis 扩展为发现多种模块
- 支持后续通过配置自动创建不同 runtime

---

## 7. 兼容策略

为了避免一次性重构引起爆炸式问题，建议使用“兼容层”方案：

### 7.1 保留旧 API

例如：

- `send_command()` 仍然可用
- 当前 `axis_*` 自定义属性接口仍然保留

### 7.2 新接口逐步接入

新增：

- `manager.apply_command()`
- `module.snapshot()`
- `module.apply_command()`

### 7.3 旧接口转发到新接口

例如：

```python
def send_command(axis_id, action, ...):
    manager = get_manager()
    return manager.apply_command(axis_id, command)
```

这样旧调用方式不需要立即改，后续再逐渐迁移。

---

## 8. 一个适合你项目的最终形态

最终你会得到类似这样的一套系统：

```text
SimulationManager
 ├── LinearAxisRuntime
 ├── RotateAxisRuntime
 └── GenericComponentRuntime

每个 runtime 由以下部分组成：
 ├── Motion Logic
 ├── Sensor / Trigger / Limit Components
 └── Blender Adapter
```

也就是说，你的项目将从“一个线性轴模拟器”演化为“一个可扩展的运动模拟框架”。

---

## 9. 结论

这次重构的重点不在“立刻把所有代码改掉”，而在于把结构从“单一功能实现”变成“统一框架骨架”。

只要先完成下面三步，就能为后续接入 RotateAxis 和更多功能件打下牢固基础：

1. 把 SimulationManager 变成真正的模块调度器
2. 把 LinearAxis 变成一个模块实现，而不是整个系统的中心
3. 为未来的 RotateAxis 和其他组件预留抽象接口