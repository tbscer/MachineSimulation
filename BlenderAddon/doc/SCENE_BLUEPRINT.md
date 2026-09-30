# 场景结构蓝图（用于在新 .blend 中重建）

> 记录自原始场景（用户在旧文件里手工搭建的嵌套 3 轴 + 1 旋转轴），
> 用途：用户新建文件后，按此蓝图重建相同结构模型。
>
> 数据取值自 Blender 实际 scene（世界坐标 / 旋转度数 / 尺寸 dims）。

## 1. 总览

- **3 个线性轴（嵌套）**：
  - `LinearAxisX`：X 方向直线轴（底座层）。
  - `LinearAxisY`：Y 方向直线轴，**挂在 X 轴的 Slider 上**（跟着 X 移动）。
  - `LinearAxisZ`：Z 方向直线轴，**挂在 Y 轴的 Slider.2 上**（跟着 Y 移动）。
- **1 个旋转轴**：`RotateAxis2`，独立放置。
- 轴宿主均为 `EMPTY`，名称必须以 `LinearAxis*` / `RotateAxis*` 开头（发现逻辑用）。

## 2. 层次结构（parent 关系，重建顺序 = 从根往下）

```
Scene
├─ LinearAxisX (EMPTY)                loc=(-2.541, -0.448, 1.600) rot=(0,0,0)
│  ├─ BasePlate (MESH)                loc=(0,0,0.15)   dims=12.0×4.0×0.3
│  ├─ Rail (MESH)                     loc=(0,-1.2,0.45) dims=10×0.2?  (见 §3 包络)
│  ├─ Sensor_Origin      (home)       loc=(-3.276,-1.925,0.567)
│  ├─ Sensor_PosLimit    (pos/back)   loc=(4.706,-1.792,0.567)
│  ├─ Sensor_NegLimit    (neg/front)  loc=(-4.759,-1.95,0.4)
│  ├─ Slider (MESH)                   loc=(-3.033,0,0.726) dims≈1.03×3.522×0.9
│  │  ├─ Slider.TriggerShim (MESH)    loc=(-3.033,0,0.726)  (shim 挂在 Slider 下)
│  │  └─ LinearAxisY (EMPTY)          loc=(-2.044,0.539,1.99)  ← Y 轴宿主
│  │     ├─ BasePlate_Y (MESH)        loc=(-3.084,0,1.859)
│  │     ├─ Rail.001 (MESH)           loc=(-2.828,0.002,1.908) rot=(180,-90,0)
│  │     ├─ Sensor.Home      (home)   loc=(-2.838,-0.722,2.509) rot=(180,-90,0)
│  │     ├─ Sensor.Limit.Pos (pos)    loc=(-2.844,1.802,2.509) rot=(180,-90,0)
│  │     ├─ Sensor.Limit.Neg (neg)    loc=(-2.838,-1.91,2.523) rot=(180,-90,0)
│  │     ├─ Slider.2 (MESH)           loc=(-2.251,-0.6,1.911) rot=(180,-90,0)
│  │     │  ├─ Slider.Shim2 (MESH)    loc=(-2.659,-0.596,2.515) (shim)
│  │     │  └─ LinearAxisZ (EMPTY)    loc=(-2.362,-0.665,1.837) ← Z 轴宿主
│  │     │     ├─ Base.LZ (MESH)      loc=(-2.378,-0.622,1.893)
│  │     │     ├─ Rail.LZ (MESH)      loc=(-2.338,-0.569,1.891) rot=(180,-90,0)
│  │     │     ├─ Sensor.Origin.LZ (home) loc=(-2.324,-0.85,1.821) rot=(180,-90,0)
│  │     │     ├─ Sensor.PosLimit.LZ (pos) loc=(-2.324,-0.85,2.220) rot=(180,-90,0)
│  │     │     ├─ Sensor.NegLimit.LZ (neg) loc=(-2.324,-0.85,1.547) rot=(180,-90,0)
│  │     │     └─ Slider.LZ (MESH)    loc=(-2.277,-0.573,1.607) rot=(180,-90,0)
│  │     │        └─ Shim.LZ (MESH)   loc=(-2.265,-0.853,1.607) (shim 挂在 Slider.LZ 下)
│  └─ (无)…
└─ RotateAxis2 (EMPTY)                loc=(2.0,-6.993,1.759) rot=(0,90,0)
   ├─ RotateBase (MESH)               loc=(3.7,-6.993,1.759)
   ├─ 旋转底座 (MESH)                  loc=(0.581,-5.813,0.45) dims≈10.4×4.1×0.275
   ├─ RotateShaft (MESH)              loc=(2.8,-6.993,1.759) dims=0.24×0.24×1.0  ← 旋转中心/轴
   │  └─ 柱体 (MESH)                  loc=(2.883,-6.738,1.467)
   ├─ Rotator (MESH)                  loc=(2.0,-6.993,1.759) dims=1.0×1.0×0.6
   │  └─ RotateTriggerShim (MESH)     loc=(2.0,-6.953,0.96) scale=(0.4,0.06,0.06) ← shim
   └─ Sensor_Home (home)              loc=(2.0,-6.993,0.379) rot=(-90,360,90)
```

> 旋转轴 `RotateShaft` 是世界 Z 向竖轴（local Z 旋转，axis_index 运行时 0~2 由检测），
> Rotator 绕它转，RotateTriggerShim 挂在 Rotator 上一起转。

## 3. 每个线性轴的 rail / 包络与配置（host 面板 cfg）

| 轴 | slider | rail | shim | home | pos_limit | neg_limit | stroke | speed | home_speed | home_dir |
|---|---|---|---|---|---|---|---|---|---|---|
| X | Slider | Rail | Slider.TriggerShim | Sensor_Origin | Sensor_PosLimit | Sensor_NegLimit | 20 | 1.0 | 0.5 | -1 |
| Y | Slider.2 | Rail.001 | Slider.Shim2 | Sensor.Home | Sensor.Limit.Pos | Sensor.Limit.Neg | 20 | 0.5 | 0.1 | -1 |
| Z | Slider.LZ | Rail.LZ | Shim.LZ | Sensor.Origin.LZ | Sensor.PosLimit.LZ | Sensor.NegLimit.LZ | 5 | 0.2 | 0.1 | -1 |

Rail 运动方向（运行时按 mesh 最长方/约定算，X=world X、Y=world Y、Z=world Z）：

- X：`rail.dir=(1,0,0)`，包络 current_x ∈ [-5, 5]（相对原点），world 位置 = 起点 + dir·x
- Y：`rail.dir=(0,1,0)`，包络 ≈ [-1.925, 1.925]
- Z：`rail.dir=(0,0,1)`，包络 ≈ [-0.372, 0.372]

Rotate 轴配置：`angular_speed=30`，`angular_home_speed=10`，`rotate_axis_index` 运行时检测（0=X,1=Y,2=Z 局部轴），rotate_center=RotateShaft、rotator=Rotator、shim=RotateTriggerShim、home=Sensor_Home，pos/neg limit 暂未配置。

## 4. 传感器的方向 / 朝向

全部传感器为 MESH，方向按 sensor 自身 local 轴（direction 属性存于 Object.sensor_direction）：
- X 轴三传感器：`rot=(0,0,0)`，direction = **X**
- Y/Z 轴与 Rotate 的传感器大多 `rot=(180,-90,0)`（local 轴被旋转）；direction 见下（建议重建后用 Sensor overlay 的白线核对，再按 local 轴选定）：
  - Y 轴（rot 180,-90,0）：local X→world +Z、local Y→world −Y、local Z→world +X → 需 local **Y** 表示 world ±Y 运动
  - Z 轴（rot 180,-90,0）：同上 → 需 local **X** 表示 world ±Z 运动
  - Rotate home（rot -90,360,90）local 映射另算，用 overlay 确认

> 注：direction 语义 = local 轴，非 world 轴；重建时以 Dev 面板 Sensor overlay（红/绿/蓝=local X/Y/Z，白=当前方向，琥珀=触发面）为准，确保白线指向实际运动法向。

## 5. 需要与 addon 发现逻辑匹配的命名/父子约定

- 每个轴宿主（`LinearAxis*` / `RotateAxis*` EMPTY）需要：
  - 线性：子级里能按 cfg 引用或按名字找到 slider / rail / shim / sensors；host cfg（linear_axis 组）自动填充后由 discovery 建 runtime。
  - shim 必须 parent 到对应 slider（TriggerShim 校验）。
- 嵌套轴：Y 宿主挂在 X 的 Slider 下、Z 宿主挂在 Y 的 Slider.2 下——addon 的 per-axis fence / ancestry 屏蔽已支持这种结构（同一载体内部不算碰撞、传感器不误认）。
- 重建后需在插件 Dev 面板点一次 **Refresh**（重新发现轴）。

## 6. 重建建议路径

新 .blend 打开后：
1. 启用 MotionSimulation 插件；
2. 用 Python（MCP execute_code / Blender 控制台）按上面 §2 层级+位置生成 EMPTY/MESH 并设置父子关系；
3. 关键 mesh（Slider/Rail/Sensors/Shim）几何尺寸用占位即可，但**名字与父子关系必须严格一致**；
4. 在 host 属性里把 slider/rail/shims/sensors 引用指向对应物体（或利用 discovery 自动按名匹配）；
5. Dev 面板 → Refresh → 用 Sensor overlay 逐个核对传感器方向。

（此文档与插件代码同目录存放，属于开发备忘。）

---

## 7. Cylinder 综合对象(详见 `doc/CYLINDER.md`)

```
Scene
└─ Cylinder (EMPTY host)             ← Cylinder*, 命名为 Cylinder1 / Cylinder_Main ...
   ├─ WorkBar (MESH, 真圆柱体)        ← 运动体,被 cylinder 驱动
   ├─ TouchShime (MESH)               ← 跟随 WorkBar 一起移动(由 runtime 同步偏移)
   ├─ ApproachSensor.Work1 (MESH)     ← 工作位置 1,虚拟立方体传感器
   ├─ ApproachSensor.Work2 (MESH)     ← 工作位置 2,虚拟立方体传感器
   └─ Base (MESH)                     ← 可选底座/装饰
```

**无 rail 部件**:运动方向由 `approach_sensor_2.world_pos - approach_sensor_1.world_pos`
推出;到位边界由 target approach sensor 的 `is_triggered` 决定。

**外部控制信号**:`host.cylinder.output_1` / `output_2` (BoolProperty) —
``(T, F)`` 走位置 1,``(F, T)`` 走位置 2,其他组合保持 IDLE。

**RPC 控制**:用 `set_outputs(module_id, output_1, output_2)` 直接写入。
**重建**:详见 `doc/CYLINDER.md` §1-§3。