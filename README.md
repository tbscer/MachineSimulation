# MachineSimulation

> **基于 Blender 的自动化设备数字孪生（Digital Twin）。**
> 在 `.blend` 场景里搭出你的机械结构，实时仿真每一根轴的运动，再用
> C# WPF 上位机去驱动 —— 不需要 PLC，不需要电柜，也不需要去车间。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Blender](https://img.shields.io/badge/Blender-Add--on-orange?logo=blender&logoColor=white)
![.NET](https://img.shields.io/badge/.NET-WPF-512BD4?logo=dotnet&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## 30 秒看懂

你在给一台自动化设备写软件 —— 可能是 pick-and-place、传送带、气缸夹紧、
旋转分度盘。你想**在电柜还没接好线之前**就把运动逻辑跑通。你想随时复现上周
二客户报的那个故障，不用飞去现场。你想带新人的时候，让他们在一个**怎么折腾
都搞不坏**的东西上面学。

**把你的运动学模型丢进 Blender，插上这个 addon，再用你日常写的 C# WPF 上位
机去控制它。** 这就是这个项目。

它不是什么花哨的工业仿真器。它就是 Blender、Python 和一个 TCP socket —— 但
够用了。

---

## 项目结构

```
tbscer/MachineSimulation/
├── BlenderAddon/      ← 仿真引擎（Blender Python addon）
├── Twin.App/          ← 上位机（C# / WPF）
└── LICENSE            Apache 2.0
```

### `BlenderAddon/` —— 仿真引擎

一个跑在 Blender 里的可插拔 runtime 框架，基于 `bpy.app.timers` 实时 tick。
在 Blender 视图里正常搭你的设备 —— 主体、滑轨、传感器 —— addon 会自动发现
并实时驱动它们运动。

| Runtime 模块 | 适用场景 |
|---|---|
| `LinearAxis` | 取放滑轨、龙门、伺服导轨 |
| `Conveyor` | 皮带输送、分段流转线 |
| `Cylinder` | 气缸夹紧、阻挡、推杆（带传感器反馈） |
| `RotateAxis` | 旋转分度盘、转塔 |
| `ApproachSensor` | 视觉 / 接近传感器 |
| `VacuumNozzle` | 真空吸盘（带 vacuum-on / vacuum-off 反馈） |

所有 runtime 都实现同一个 `BaseSimulationModule` 接口 —— 自己写一个，丢进去，
`SimulationManager` 就会把它当成其他模块一样对待。碰撞检测统一走
`CollisionEngine.step()`，每个模块拿到的是同一份答案。

### `Twin.App/` —— 上位机

一个 C# WPF 应用，通过**仅本机**的 JSON-over-TCP RPC（`127.0.0.1:9877`）跟
addon 通信。不绑 LAN，不带鉴权，**只走 loopback**。

| 项目 | 作用 |
|---|---|
| `Twin.MotionInterface` | 硬件抽象接口（`IAxis`、`ICylinder`、`IMotionCard` 等） |
| `Twin.Communicate` | 连 addon 的 `BlenderRpcClient` |
| `Twin.MotionSimulate` | 仿真运动控制卡，本地镜像 addon 状态 |
| `Twin.Application` | WPF 窗口、ViewModel，最终跑起来的运控编排 |

---

## 整体架构

```
   ┌─────────────────────────┐                ┌──────────────────────────┐
   │     Twin.App  (C#)      │  JSON-over-TCP  │   BlenderAddon  (bpy)   │
   │                         │ ◄─────────────► │                          │
   │  ┌──────────────────┐   │   127.0.0.1     │   ┌──────────────────┐   │
   │  │  SimMotionCard   │   │      :9877      │   │ SimulationManager│   │
   │  │  ├ SimAxis       │   │                 │   │  ├ LinearAxis    │   │
   │  │  ├ SimCylinder   │   │   Home/Move/    │   │  ├ Conveyor      │   │
   │  │  └ SimInputIO    │ ◄─┼────────────────►│   │  ├ Cylinder      │   │
   │  └──────────────────┘   │                 │   │  ├ RotateAxis    │   │
   │                         │   state push    │   │  └ …             │   │
   │  WPF MainWindow         │ ◄────────────── │   └──────────────────┘   │
   │  MainWindowViewModel    │                 │            │              │
   └─────────────────────────┘                 │            ▼              │
                                               │    Blender 视图窗口       │
                                               │    （实时动画）           │
                                               └──────────────────────────┘
```

整条链路就是：**Blender 里建模 → Blender 里仿真 → C# 里控制**。没有中间格式、
没有 STEP 导入管线、没有厂商 SDK。

---

## 三步上手

1. **装 addon。** Blender 里 *编辑 → 偏好设置 → 插件 → 安装…*，选
   `BlenderAddon/__init__.py`，然后启用 *MachineSimulation*。
2. **打开示例场景。** 加载 `examples/New Generate Model.blend`（见下方
   "示例场景" 说明），或者自己搭一个 —— 把运动部件按命名规则 parent 到对应
   对象上，addon 会自动发现。
3. **编译并跑 WPF 上位机。** Visual Studio 打开
   `Twin.App/Twin.Communicate.sln`，编译运行，点 *Connect*，就可以在 UI 上
   对仿真轴做 Home / Move / Jog。

> 第一次看代码？[`BlenderAddon/doc/PROJECT_INDEX.md`](BlenderAddon/doc/PROJECT_INDEX.md)
> 是一份精选的设计文档导览。

---

## 这个项目能干什么

- ✅ **离线开发运控序列。** 没有 PLC、没有伺服、没有 I/O —— 只有 Blender。
- ✅ **复现客户报的故障。** 同一个 `.blend`、同一段序列，每次都一样。
- ✅ **培训操作员和销售工程师。** 让他们在一个**怎么折腾都搞不坏**的设备上练。
- ✅ **逐帧回放序列。** 直接用 Blender 时间轴。
- ✅ **单元测试你的 `SimAxis` / `SimCylinder` 逻辑。** 不用每次都搭硬件。

## 这个项目不是什么

- ❌ **不是实时控制器。** 主循环是 `bpy.app.timers`，不是硬实时系统。
- ❌ **不是物理引擎。** 碰撞是基于 AABB 的，详见
   [`doc/COLLISION_GUIDE.md`](BlenderAddon/doc/COLLISION_GUIDE.md)。
- ❌ **不替代安全 PLC。** 急停老老实实接到真实硬件上。
- ❌ **不是多用户 / 网络化仿真器。** RPC 故意只走 loopback。

---

## Roadmap

- [ ] 更多 runtime 模块 —— VisionSensor、RFID reader、力矩传感器
- [ ] WPF 端做图形化序列编辑器
- [ ] 日志回放用于故障定位
- [ ] 多设备场景组合

---

## 示例场景

📎 **`New Generate Model.blend`** —— 一个配好的示例，里面有一个直
线轴、一条传送带、一个气缸，三者联动。Clone 下来之后丢到项目根目录的
`examples/` 下即可（因为是二进制文件，所以是单独上传的，git 里不跟踪）。

---

## 参与贡献

欢迎提 Issue 和 PR。如果想加新的 runtime 模块，门槛很低：实现
`BaseSimulationModule`，在 `BlenderAddon/modules/__init__.py` 里注册一下，
框架剩下的部分会自动接管。

## 协议

Apache License 2.0 —— 见 [`LICENSE`](LICENSE)。
