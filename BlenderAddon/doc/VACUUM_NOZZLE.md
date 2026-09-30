# Vacuum Nozzle 综合对象

> 把 `vacuum_nozzle` 模块的架构与用法集中记在这里 —— 适用于“真空吸嘴”
> 类的拾取/搬运应用：**开真空（On）** 把感应区内的物体吸成自己的子对象，
> 吸嘴被别的对象（通常是挂载它的轴）带着运动时它们跟着走；
> **关真空（Off，默认态）** 把子对象全部拆解回场景目录。

## 1. 组件结构

```
VacuumNozzle1 (MESH, 通常画成一个圆柱体)   ←── host:配置容器 + 感应区锚点 + 被吸物体的新父级
                                              （自己的位置/旋转 = 感应区的坐标系）
Workpiece1 (MESH)                          ←── 场景里任意可吸物体（不属于机器）
Workpiece2 (MESH)
```

**没有 `sensor_mesh`** —— 吸嘴自己就是感应区的锚点。这一点与
`ApproachSensor` 不同（后者的立方体挂在独立的 sensor mesh 上），
但**几何模型完全一致**（见 §2）。

host 识别靠**名字前缀** `VacuumNozzle`（`modules/VacuumNozzle/naming.py::is_host`），
**不限制 `obj.type`** —— MESH / EMPTY 都可以。选中它就能在
`PROPERTIES > Object > Vacuum Nozzle` 看到控制面板（需求 3）。

## 2. 感应区（与 ApproachSensor 同构）

### 2.1 立方体几何（host 局部坐标系）

- **起点**：`working_face_center`（Vector3，默认 `(0, 0, 0)`）
- **沿法向**：从 `working_face_center` 向 `+normal_axis` 方向延伸
  `cube_size[normal_axis]`，默认 3
- **横截面**：以 `working_face_center` 为中心，
  `cube_size[i] × cube_size[j]`，默认 `1 × 1`

`cube_size` 默认 `(1, 1, 3)`，配合默认 `normal_axis = Z` 即：
横截面 1×1，沿 host 局部 +Z 方向长 3（场景 `scale_length = 0.001` 时即 1×1×3 mm）。

### 2.2 RNA 属性（挂在 host 自己身上）

| 属性 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `vacuum_nozzle_normal_axis` | Enum X/Y/Z | Z | 法向轴 |
| `vacuum_nozzle_cube_size` | FloatVector(3) | (1, 1, 3) | `[normal_axis]` 是沿法向长度，另两维是横截面 |
| `vacuum_nozzle_working_face_center` | FloatVector(3) | (0, 0, 0) | host 局部坐标系下的立方体起点 |

属性注册在 `bpy.types.Object` 上（`modules/components/vacuum_nozzle/rna.py`），
所以任何对象都能编辑；运行时只从 host 读，并且**每 tick 重新读取**
（`VacuumNozzleSensor.refresh()`），面板里改完立刻生效。

### 2.3 检测算法

把候选物体的 8 个世界 AABB 角点变换到 host 局部系 → OBB；立方体在 host
局部系里是 AABB。SAT 判定 15 个分离轴（3 个候选 OBB 轴 + 3 个立方体 AABB 轴
+ 9 个叉积），任一轴出现分离 → 不相交。几何 helper 直接复用
`modules/components/approach_sensor/approach_sensor.py` 的私有函数。

### 2.4 视口可视化

overlay 按名字前缀识别 host（`modules/components/vacuum_nozzle/overlay.py`），
**不需要**任何 custom property 标记 —— 即使吸嘴还没开真空（未注册 runtime），
overlay 照样画得出来，便于先把感应区调好再 On。

- 立方体 12 条棱线框 + 6 面半透明填充
- 法向白线：从 `working_face_center` 沿 `+normal_axis` 延伸
  `cube_size[normal_axis] + 5`，便于确认指向

颜色由 runtime 每 tick 写回 host 的三个 bool 决定：

| `vacuum_nozzle_on` | `vacuum_nozzle_holding` | 颜色 | 含义 |
|---|---|---|---|
| False | — | 绿 | Off（默认态） |
| True | False | 琥珀 | On，感应区内暂无物体 |
| True | True | 青 | 已吸住物体 |

**显示策略：永远显示**。本 overlay **不受**
`Scene.ms_show_sensor_overlay` / `Scene.ms_sensor_overlay_live` 影响 ——
那两个开关是给 `UTypeSensor` / `ApproachSensor` overlay 用的。感应区盒子
是你调区域的**工作界面**，被别的开关连坐关掉会让人以为功能坏了。

刷新由 0.25s timer 驱动，但采用**变化检测**：只有当“外观输入”真的变了
（host 名字 / on / holding / 可见性 / 感应区几何 / world 矩阵）才
`area.tag_redraw()`。所以它始终跟得上变化（包括被轴带着运动、开关真空、
在面板里改尺寸），又不会让视口永久 4Hz 空转。

想临时藏掉某个吸嘴的盒子：把那个对象在 Outliner 里眼睛图标关掉
（`hide_viewport`）—— 逐对象的显式意图，overlay 会尊重。

## 3. VacuumNozzleModule —— 状态机

```
DISABLED (=Off, 默认) ──set_enabled(True)──> IDLE
IDLE (On, 未吸住)      ──感应区内有候选──> HOLDING
HOLDING (On, 已吸住)   ──继续吸新进入的候选(不自动释放)──> HOLDING
IDLE / HOLDING         ──set_enabled(False) / Off──> DISABLED（释放全部）
*                      ──collision engine 命中──> BLOCKED
BLOCKED                ──reset_collision / marker 清除──> DISABLED / IDLE
```

### 3.1 关键语义

- **吸附是 sticky 的**：一旦吸住就一直持有，只有 Off（或 `force_release()`）
  才释放 —— 这就是真实真空吸嘴的语义（开真空吸住，关真空丢掉）。吸住后
  工件即使（相对吸嘴）离开感应区也不会被自动丢掉。
- **Off 立即释放**：`set_enabled(False)` 当场拆父子，不必等下一个 tick。
- **Off / force_release 不属于运动**，因此在 **BLOCKED 期间也会执行**，
  避免工件被永久粘住。
- **world 位姿**：attach 与 release 都保留**当前** world 位姿。
  注意 Blender 的 `parent = None` **不**自动保留 world（`matrix_world`
  会退化成 `matrix_basis`，也就是挂上去那一刻的位姿），所以 `_release`
  必须自己备份并写回 —— 否则吸嘴带着工件跑一段后 Off，工件会弹回起点。
- **一次可以吸多个**：感应区内所有候选都会被吸住（不是只吸最近的一个）。
- 释放后物体**不**回到原来的父级，而是落到场景目录下（需求 2）。

### 3.2 每 tick 决策

1. 引用存活检查（host 被删 → `_alive = False`）
2. 镜像 cfg `enabled` → instance
3. **Off 路径**：释放全部 → DISABLED（放在 collision 门控**之前**）
4. **全局 collision 门控**：marker 已清且自己 BLOCKED → 自动解锁；
   marker 置位 → `_enter_blocked_state()` 并放弃本 tick
5. held 一致性检查（stale / 被外部 unparent → 清掉）
6. **On 路径**：`sensor.refresh()` → 扫描候选 → 吸附新的（只加不减）
7. 写回 `vacuum_nozzle_on` / `_holding` / `_sensing` 给 overlay

## 4. 哪些物体能被吸（候选筛选）

**自动扫描**场景 —— 没有“指定目标”的配置项（旧版的 `trigger_obj` 已彻底
删除，理由见 §4.1）。候选必须满足：

1. 不是 host 自己；
2. `type == "MESH"`；
3. **不是 host 的祖先** —— 不能把挂着自己的载体 / 底座吸走；
4. 没有 `sensor_type` 自定义属性（传感器属于机器）；
5. 名字不是 `VacuumNozzle*`（别的吸嘴属于机器）；
6. 不在**其它模块声明**的机器部件集合里 —— 取各模块
   `collision_structure().members` 的并集。注意是“声明”而不是“子树”：
   `Base` 这种挂在 Cylinder host 下但**没被声明**为机器部件的工件
   **仍然是可吸的**；
7. 没有被别的真空吸嘴持有（`obj.parent` 是另一个 `VacuumNozzle*`）。

### 4.1 为什么删掉了 `trigger_obj`

旧版有一个可选的 `trigger_obj`（“只吸指定的那一个对象”）。它是个**静默
陷阱**：

- 更旧的版本里它是**必填**字段，所以历史 `.blend` 里普遍残留着它；
- 新版里它是“唯一目标白名单”，一旦填了就**整体绕过自动扫描**；
- 于是同一个场景里，没填的吸嘴能吸，填了（但目标早就不在感应区里）的
  吸嘴“什么都吸不到”，而且面板上只有一个毫不起眼的指针字段 —— 极难归因。

实测过的 2×2 对照（同一个吸嘴、同一位置，只改“父级”与“trigger_obj”）：

| host.parent | trigger_obj | 候选集 | 结果 |
|---|---|---|---|
| WorkBar（Cylinder 下） | 空 | 全部可吸对象 | 吸住 ✓ |
| 场景目录 | 空 | 全部可吸对象 | 吸住 ✓ |
| WorkBar | 指定对象 | **只有那一个** | 吸不到 ✗ |
| 场景目录 | 指定对象 | **只有那一个** | 吸不到 ✗ |

父级完全无影响；决定成败的是 `trigger_obj`。现在只剩一条路径，歧义消失。

> **已知限制**：规则 6 只看“声明”，所以某个模块 host 下未声明的**机器**
> 附件（例如挂在 LinearAxis 下的装饰件）如果恰好落进感应区，会被吸走。
> 实践中吸嘴的感应区很小且对准工件，这类误吸可以靠几何位置避免。

## 5. collision 语义

```python
members = (host, *当前吸住的物体)   # bodies = ()
```

- **bodies 为空** —— 吸嘴不产生独立运动，自身不参与碰撞检测
  （它的运动由挂载它的轴负责）。
- 吸住期间工件是模块成员，与 host 同组 → **不会被吸嘴自己的载体判定为撞**。
- **Off 释放后工件退回场景目录**，重新变成普通 mesh —— 归入自动组合或被
  `uncovered` 抓出来，与其它轴正常互撞（“工件在台上，别的轴压上去仍然是撞”）。
- 旧版的 `trigger_obj` “**声明即永久属组**” 语义已**取消**，字段本身也已
  删除（见 §4.1）；`sensor_mesh` 同样已从声明中移除。

## 6. 配置面板

`OBJECT_PT_vacuum_nozzle`（`PROPERTIES > object`，选中物体名字以
`VacuumNozzle` 开头时显示）：

- **Vacuum control** —— `enabled`（= Vacuum On toggle）+ `Refresh`
- **Sensing area** —— 直接编辑 host 自己的
  `normal_axis` / `cube_size` / `working_face_center`（overlay 实时显示同一几何）
- **Mounted on** —— 只读显示吸嘴被谁带着运动（`obj.parent`）
- **Runtime state** —— `state` / `on` / `sensing` / `holding`(数量) /
  已吸物体名单 / `stop_reason`，以及 `Force Release` 按钮

Dev 面板有独立的 **Vacuum Nozzles** 子面板（只读状态回显）。

> **不需要先开真空才注册**：discovery 只看 host 身份 + `vacuum_nozzle`
> PropertyGroup 是否存在，**不看** `enabled`。`enabled`（Vacuum On/Off）
> 是纯运行时状态，每 tick 镜像 cfg，点 On 后下一个 tick 就开始扫描吸附
> （≤1/30 s），RPC `set_vacuum_enabled` 同理。
>
> 之所以不按 `enabled` 门控：`enabled` 默认是 **False**，而 discovery 是
> **一次性**的（插件 enable / 点 Refresh）。若按它门控，"先开插件、再点
> Vacuum On"这条最普通的路径里 discovery 早就跳过了吸嘴，模块永不注册，
> 点 On 之后**什么都不发生**（不吸附也不报警）。**Refresh** 只用于
> "新建 / 重命名 / 重新挂载了吸嘴"。

## 7. RPC 集成

| method | 参数 | 返回 | 说明 |
|---|---|---|---|
| `set_vacuum_enabled` | `module_id`, `enabled`(bool) | `{"enabled": bool}` | On/Off |
| `force_release_vacuum` | `module_id` | `{"released": bool}` | 强制释放，**绕过** BLOCKED 门控 |

通用 `apply_command` 也支持内部 action：`set_enabled` / `force_release` /
`idle` / `stop` / `reset_collision`。BLOCKED 期间只放行
`set_enabled` / `force_release` / `reset_collision`。

snapshot 字段见 `doc/RPC.md` §5.2；注意 `sensor_mesh_name` 与
`trigger_obj_name` 均已移除。

## 8. 测试覆盖

- `tests/test_vacuum_nozzle.py` —— 几何 / 默认值 / 边界 / 退化 / `refresh()` /
  host 命名
- `tests/test_vacuum_nozzle_runtime.py` —— 状态机 / 多物体吸附 / sticky /
  Off 释放(含 BLOCKED) / attach-release 保留 world / **放下不弹回** /
  候选筛选 7 条规则 / **trigger_obj 已删除**的三道门 / snapshot /
  collision 门控 / 结构声明 / 多次 pick-release 循环

总计 67 个离线用例，无需 Blender 即可验证。

## 9. 已知限制

- **BLOCKED 期间不扫描**：进 BLOCKED 后本模块短路，不再吸附新物体；
  已经吸住的保持（要丢件请 Off 或 `force_release`）。
- **规则 6 只看声明**（见 §4 的已知限制）。
- **不恢复原父级**：Off 释放后一律落到场景目录，不回原 parent。