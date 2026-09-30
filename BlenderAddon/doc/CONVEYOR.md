# Conveyor 传送带

> 把 `conveyor` 模块的架构与用法集中记在这里 —— 适用于"传送带 / 流水线"
> 类应用:由一个 `drive_roller`(主动轮)和一个 `idler_roller`(从动轮)
> 撑起一条静态 `belt` mesh。`running` toggle 控制启动/停止,
> `direction` 决定传送方向,`friction` 控制抓地系数,
> `target_speed` 设置线速度。runtime 每 tick 找到接触 belt 的刚体
> 物体并修改其 `linear_velocity` / `location`(Blender 5.x 路径),
> 让物体以"皮带速度 × friction"的速率被推动;
> 同时往 belt mesh 写 `belt_uv_offset` 自定义属性供材质 Mapping 节点
> 滚动纹理。

## 1. 组件结构

```
Conveyor1 (EMPTY host)                       ←── host, 命名 Conveyor*
   · drive_roller (MESH, 真圆柱体)           ←── 主动旋转轮(决定传送方向)
   · idler_roller (MESH, 真圆柱体)           ←── 从动轮(仅支撑)
   · belt (MESH, 长方体 mesh)                ←── 静态传送带(承载物体 + 纹理滚动锚点)

外部:
   CubeA (MESH, RigidBody)                    ←── 场景里任何带 RigidBody 的物体
   CubeB (MESH, RigidBody)                       接触 belt 时会被驱动
```

**Conveyor 不动自己** —— 不像 LinearAxis / Cylinder / RotateAxis
那样更新自身某个部件的 `location`,而是**修改场景里其它刚体物体**的
运动参数。这是设计上与 axis 类最大的不同(同 VacuumNozzle / Sensor 一样
`bodies=()`,只是它们更被动,Conveyor 是**主动驱动外部物体**)。

host 识别靠**名字前缀** `Conveyor`(`modules/Conveyor/naming.py::is_host`)。
host **必须**是 EMPTY(同 axis 类);drive / idler / belt 是任意 MESH。

## 2. 配置面(`ConveyorProperty`)

挂在每个 `bpy.types.Object.conveyor` 上(`modules/Conveyor/component.py`),
artist 在 Object Properties → Conveyor 面板里编辑,离线测试 / MCP 直接
写属性也立刻生效(runtime `_sync_from_cfg` 每 tick 重读)。

### 2.1 必填 Pointer(3 个)

| 字段 | 类型 | 说明 |
|------|------|------|
| `drive_roller` | Pointer(Object) | 主动轮 mesh(常画成 cylinder)。`direction="forward"` 时物体从这里被推向 idler。 |
| `idler_roller` | Pointer(Object) | 从动轮 mesh。不主动旋转,仅作支撑。两 roller **必须不同 Object**,否则 discovery 跳过。 |
| `belt` | Pointer(Object) | 皮带 mesh(常画成长方体)。runtime 往它身上写 `belt_uv_offset` 自定义属性,供材质 Mapping 节点用。 |

**约束**:
- 两个 roller **不能是同一个 Object**(同 Cylinder approach_sensor 校验)。
- 两个 roller 世界位置 **不能重合**,否则构造期 raise `ValueError`。
- belt 可以与 roller 重合 host(虽然不推荐)。

### 2.2 运动参数

| 字段 | 类型 | 默认 | 单位 | 说明 |
|------|------|------|------|------|
| `target_speed` | float | 50.0 | mm/s | 线速度。内部按 `0.001` 系数换算成 BU/s(`scale_length=0.001` 场景下 1 BU = 1 mm,与 LinearAxis / Cylinder 同)。 |
| `direction` | Enum | `"forward"` | — | 传送方向。`forward` = drive → idler(+1);`reverse` = idler → drive(−1)。切方向不改变 `belt_dir`,只翻速度符号。 |
| `friction` | float | 1.0 | [0, 1] | 抓地系数。`1.0` = 完全抓地(物体速度瞬间匹配皮带);`0.0` = 完全打滑(物体不被推动);中间值 = 物体当前速度沿 `belt_dir` 一维 lerp。越界截断。 |

### 2.3 控制

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enabled` | bool | True | 总开关。`False` 时 discovery 跳过该 host,模块不被注册。 |
| `running` | bool | False | 启停 toggle。`True` = 启动(`STATE_RUNNING`);`False` = 停止(`STATE_IDLE`,不扫场景、不写 `linear_velocity`、不写 `location`)。**uv_offset 仍按当前速度累加**(artist 调过速度后再 stop 设置仍保留)。 |

## 3. ConveyorModule 运行时(`modules/Conveyor/runtime.py`)

### 3.1 状态机

```
IDLE (running=False)
   │
   ├──set_running(True)──> RUNNING
   │
RUNNING
   │
   ├──set_running(False) / stop / idle──> IDLE
   │
   ├──collision engine 命中 / scene marker 置位──> BLOCKED
   │
BLOCKED
   │
   ├──reset_collision / marker 清除 + running=False──> IDLE
   ├──reset_collision / marker 清除 + running=True ──> RUNNING
```

**collision 契约**(与 LinearAxis / Cylinder / VacuumNozzle 同构):

1. **不检查自身碰撞**:`collision_structure().bodies = ()` —— conveyor
   不产自身运动,只驱动场景其它刚体。
2. **全局停车**: 命中且引擎启用时进 `BLOCKED` +
   `stop_reason="collision"`,并 `engine.mark_collision()` 写
   `scene["motion_simulation_collision"]`;**其它 module** 下一 tick 读到
   该 marker 也会进 BLOCKED。
3. **BLOCKED 守卫**:`apply_command` 顶部守卫只放行
   `set_running` / `set_speed` / `set_direction` / `set_friction` /
   `start` / `reset_collision`(状态位类允许);`stop` / `idle` 会改
   `state`,被拒。
4. **自动解锁**: 别人调 `reset_collision` 清掉 marker 后,本模块下一 tick
   自行退出 BLOCKED。
5. **引擎禁用 = kill switch**:`engine.is_enabled == False` 时仍跑 BVH
   检查(供 Dev panel 显示 `last_collision`),但**不会**重新锁进 BLOCKED;
   同时 `CollisionEngine.set_disabled` 会主动调本模块的
   `_clear_blocked_state_only` 立即释放。

> **关键不变量**:`_enter_blocked_state()` 仅改本地 `state` / `stop_reason`,
> **不写 scene marker**。marker 由"发现碰撞的那一方"写(集中式拉模式下
> 是 `CollisionEngine.step`)。命名 `_clear_blocked_state_only` 不可改 —
> `CollisionEngine.set_disabled` 按方法名遍历模块。

### 3.2 几何推导(`__init__` 期)

```python
# 默认 forward 语义: drive → idler,即 +belt_dir 方向
delta = (idler.world_pos - drive.world_pos).normalized()  # belt_dir
length = |idler - drive|                                   # belt_length
```

- `belt_dir`: 单位向量,world 空间(默认 drive 在 (0,0,0)、idler 在
  (L,0,0) 时为 `(1, 0, 0)`)。
- `belt_length`: 两 roller 间距,供 uv_offset 缩放。
- 两 roller 世界位置重合 → raise `ValueError`(discovery 阶段已挡)。

### 3.3 每 tick(`update(dt)`)

```
1. 引用存活检查(host / 两 roller / belt 任一 stale → _alive=False, manager unregister)
2. 镜像 cfg(target_speed / direction / friction / running)→ instance
3. 全局 collision 门控(marker 置位 → 进 BLOCKED 并 return,但仍更新 uv_offset)
4. 本地 BLOCKED 短路(同 step 3,仍更新 uv_offset)
5. running=False → 不扫场景,不写物体;返回
6. v_belt = belt_dir * direction * target_speed (BU/s, 不做 mm→BU 换算)
7. v_belt 长度 ≤ 1e-9 → 不扫场景,不写物体;返回
8. _scan_objects_on_belt() → 被驱动物体集合
9. 每个 obj 调 _apply_velocity(obj, v_belt, dt)
10. _update_belt_uv_offset(dt) → belt["belt_uv_offset"] += ...
```

### 3.4 接触检测(`_scan_objects_on_belt`)—— **三重过滤**

每 tick 从场景树顶层 root 出发递归遍历(不走 flat 列表),用三重过滤
定位"接触 belt 的独立刚体":

1. **场景树遍历 + 已认领跳过**:从 `scene.objects` 的顶层 root 开始递归。
   若某节点属于"已认领的 host"集合(自己模块的 host_obj / belt / 两 roller,
   或其它已注册 module 的 host_obj),**整棵 subtree 跳过**——避免推动
   LinearAxis slider / VacuumNozzle 内部物体这种隐式联锁。
2. **类型过滤**:被遍历到的对象必须是 `type == "MESH"` 且装上 `rigid_body`;
   LinearAxis1 / Cylinder1 / VacuumNozzle1 等 EMPTY host(即使有 rigid_body)
   因为 type 不匹配也被跳过。
3. **面接触**:对象的任一顶点 Z 落在
   `[belt_bot - h_tol, belt_top + h_tol]` 范围内、且该顶点在 `belt_dir`
   上的投影落在 `[proj_min, proj_max]` 区间内 → 即认为该物体有一个面与
   belt 表面接触。`h_tol = DEFAULT_HEIGHT_TOLERANCE_BU = 0.05`(5cm)。

实现拆为三个独立辅助函数(模块级):

```python
def _compute_belt_contact_zone(belt_mesh, belt_dir):
    """(belt_top_z, belt_bot_z, belt_center, proj_min, proj_max)"""

def _collect_free_recursive(obj, skip_set, candidates):
    """递归收集 free mesh + rigid_body 对象,遇 skip 集合节点整棵停止。"""

def _iter_world_vertices(obj):
    """迭代 obj 的世界空间顶点。优先 mesh.vertices,退回 bound_box 8 角点。"""
```

类内方法仅保留 `_has_face_contact()` 一次性接触检测 + `_collect_skip_set()`
收集 host 集合。其余"扫描骨架"全部由模块级函数组合,可读性大幅提升。

> **与 VacuumNozzle 的区别**:VacuumNozzle 通过把物体 reparent 到自己来
> "抓住"它;Conveyor 通过写 `linear_velocity` / `location` 来"推"它。
> 两者都不修改其它 module 的部件,只把"自由刚体"作为操作对象。

### 3.5 位置推动(`_apply_velocity`，1a 阻挡收缩)
> 2024 会话决策：驱动**回退位置计算**（不走 Bullet/摩擦）。
> 配套：manager 物理时钟**默认关闭**（dvance_frame=False，纯逻辑模式）——
> 不推帧、Bullet 不步进、时间线不跑；需要 dynamic 刚体受重力时再置 True。
> Phase 2 物理驱动（移动皮带刚体靠摩擦带动、wrap 回绕）已删除。

每 tick 对每个接触物体：

1. **kinematic 持有**(`_sync_drive_kinematic`)：被驱动期间置 `rb.kinematic=True`
   —— Blender 5.x 下 ACTIVE 非 kinematic 刚体的 transform 归 Bullet，直接写
   `obj.location` 会被仿真回写覆盖（线上实测）；kinematic 状态写入立即生效。
   离开皮带 / 停止 / BLOCKED 时还原原值（此时 Bullet 按最近运动推断速度，
   每 tick 一小步 → 推断速度 ≈ 皮带速度，"带着速度离开皮带"语义自然）。
2. **friction lerp**：沿 belt_dir 一维 lerp（friction=1 完全抓地，0 打滑不动）。
3. **1a 阻挡收缩**(`_clamp_delta_by_blockers`)：目标位移会侵入其它**刚体**
   （带 `rigid_body` 的 MESH，排除自身父子链与 conveyor host 子树）时，
   二分收缩到位移前刚好不重叠 —— "顶住停在接触处，每 tick 重试，对面让开
   自动续走"。贴合支撑面（轴向侵入≈0）不判阻挡。
   AABB 口径必须用**真实网格顶点**(`_obj_world_vbox`)：`bound_box` 在部分
   对象上与网格尺寸差出数倍（线上实测 TestBoard），会误判"已顶死"死锁。
4. **位置写入**：`obj.location += belt_dir × v × dt`（走满目标速度）；
   ≤4.x 的 `rb.linear_velocity` 写路径保留（速度差补偿口径）。

### 3.6 视觉纹理滚动(`_update_belt_uv_offset`)

每 tick 把 belt mesh 的 `belt_uv_offset` 自定义属性累加:

```python
delta = direction * target_speed * dt * uv_scale
belt["belt_uv_offset"] += delta
```

- `uv_scale = 1.0 / belt_length`(默认 `DEFAULT_UV_SCALE = 1.0`),让纹理
  滚动速率视觉上与"皮带表面速率"匹配。
- **永远累加**,不因为 Start/Stop 重置。artist 调过速度后再 stop 设置
  仍保留(纹理按当前速度继续滚)。
- 不被 BLOCKED 短路(视觉不卡)。

材质侧:`belt_uv_offset` 是单 float,供材质 Mapping 节点的 Location.x
用 Driver 接(详见 §6)。

## 4. RPC 接口

### 4.1 顶层 method `set_conveyor_running`

与 `set_outputs` / `set_vacuum_enabled` 同构 —— 独立顶层 RPC method,不靠
通用 `apply_command` 走 action 词表。所有参数都是可选,缺省则不改动
对应字段,方便"只改速度"或"只改方向"的局部修改。

```json
{
  "id": 14,
  "method": "set_conveyor_running",
  "params": {
    "module_id": "Conveyor1",
    "running": true,
    "direction": 1,
    "target_speed": 50.0,
    "friction": 1.0
  }
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `module_id` | string | 是 | Conveyor host 名 |
| `running` | bool | 否 | 启停 |
| `direction` | int ∈ {-1, +1} | 否 | 传送方向 |
| `target_speed` | number ≥ 0 | 否 | 线速度 mm/s |
| `friction` | number ∈ [0, 1] | 否 | 抓地系数 |

响应(从 snapshot 读,反映 cfg 已镜像):

```json
{"ok": true, "id": 14, "result": {"running": true, "direction": 1, "target_speed": 50.0, "friction": 1.0}}
```

错误码沿用 `INVALID_PARAMS` / `AXIS_NOT_FOUND` /
`AXIS_BLOCKED`(与现有其它顶层 method 一致)。**不被 BLOCKED 门控拒**:
状态位类(4 个 setter + `reset_collision`)允许在 BLOCKED 期间写入。

### 4.2 通用 `apply_command`

走 axis 模块统一的 `apply_command(action, payload)` 入口,
Conveyor 接受以下 action:

```
("set_running", "set_speed", "set_direction", "set_friction",
 "start", "stop", "idle", "reset_collision")
```

`start` ≡ 顶层 `set_conveyor_running(running=true, ...)`:payload 字段
同名同序(`target_speed` / `direction` / `friction`,全可选,缺省不改),
唯一差异是 `running` 缺省为 `true`(这正是 start 的语义)。示例:

```json
{"action": "start",
 "payload": {"target_speed": 50.0, "direction": 1, "friction": 1.0}}
```

BLOCKED 门控守卫:仅放行 `BLOCKED_ALLOWED_ACTIONS = ("set_running",
"set_speed", "set_direction", "set_friction", "start",
"reset_collision")` — 状态位类允许;`stop` / `idle` 会改 `state`,
被拒(避免绕过碰撞锁)。

### 4.3 state_push 桶

`category="axes"` —— 与 LinearAxis / RotateAxis / Cylinder 同桶,
出现在 `data["axes"]` 里。每条 snapshot 含:

| 字段 | 类型 | 说明 |
|------|------|------|
| `module_id` | string | Conveyor host 名 |
| `kind` | string | 固定 `"conveyor"` |
| `name` | string | 同 `module_id` |
| `state` | string | `"idle"` / `"running"` / `"blocked"` |
| `running` | bool | 镜像 cfg |
| `direction` | int | +1 forward / -1 reverse |
| `target_speed` | number | mm/s |
| `friction` | number | 0..1 |
| `driven_names` | string[] | 被驱动物体名列表 |
| `stop_reason` | string | `""` / `"running"` / `"cmd_stop"` / `"cmd_idle"` / `"invalid_direction"` / `"collision"` |

## 5. 视觉与材质(自动设置)

### 5.1 物体驱动 — 无自动 setup

被驱动物体不需要额外配置 — 它只要:

1. `obj.type == "MESH"`
2. `obj.rigid_body` 装上(默认即有)
3. 放在 belt 上 / 在 belt 上方且 Z 距离 ≤ 5cm

不需要 `sensor_type` / 任何 custom property 标记。

### 5.2 皮带纹理 — 手动接 Driver(推荐)

artist 在 belt mesh 的材质里设置:

1. Image Texture 节点 + Mapping 节点 + Principled BSDF
2. Mapping 的 Location.x 用 Driver 绑到 `obj["belt_uv_offset"]`
   (Add Driver → Single Property → `obj.belt_uv_offset`)

每次 runtime tick 累加一次,belt 纹理沿 `direction` 方向滚动,
视觉上看得出"传送带在跑"。

### 5.3 自动材质设置 operator(可选便捷按钮)

UI 面板里的 "Setup Belt Material" 按钮(plan 中设计,当前未在 v1 实现,
可作为 future work;本节作为 future reference)。

## 6. UI 面板

### 6.1 Object Properties → Conveyor(host 选中时显示)

判定:`is_host(obj)`(同 discovery 共用,避免 drift)。

分组:

- **Host references**: `drive_roller` / `idler_roller` / `belt` 三个
  PointerProperty。
- **Motion**: `target_speed` (mm/s) / `direction` (Forward/Reverse) /
  `friction` (0..1)。
- **Control**: `Running` toggle + `Refresh` (重新发现) + `Apply`
  (把 4 个 cfg 字段推回 instance)。
- **Runtime state**(只读): `state` / `running` / `direction` /
  `target_speed` / `friction` / `uv_offset` / `driven_count` +
  `driven_names` + `stop_reason`。

### 6.2 N-Panel → MotionSimulation Dev → Conveyors

新增 `MS_PT_dev_conveyor` 子面板,列出每个注册的 `ConveyorModule`,
展示 `state` / `running` / `direction` / `target_speed` / `friction` /
`uv_offset` / `driven_count` + `driven_names`(走 `_draw_conveyor_body`,
与其它 kind 各画各的)。

子面板按 kind 字母排序:`Conveyors` 排在 `Cylinders` 之后。

## 7. collision 契约实现细节

### 7.1 `collision_structure()`

```python
members = (host, drive_roller, idler_roller, belt)  # 任一为 None 时跳过
bodies  = ()  # conveyor 不产自身运动
```

- members 含 4 个,**组内任意两对象永不互撞**(R1)。
- bodies 空 —— `bodies=()` 意味着 conveyor 不参与碰撞检测(同 VacuumNozzle
  风格),只参与 scene marker 全局阻塞门控。
- 被驱动的物体(场景里装 RigidBody 的 cube / box)**不**进 members:
  它们不在 conveyor 的保护伞下,被其它模块撞到仍算真碰撞。这是 design
  choice —— "声明即永久免疫"的旧版行为在 VacuumNozzle 已被移除。
- 自动结构 (house-keeping): 未声明的根节点由 engine 自动包成
  `structure:<根名>`(`host=None`, `bodies=()`)。被驱动物体若没被任何
  module 声明,会落到自动组合里,跨组检测正常。

### 7.2 关键不变量

1. `_enter_blocked_state()` 不写 scene marker(由 engine 写)。
2. BLOCKED 守卫对称(`set_running` / `set_speed` / `set_direction` /
   `set_friction` / `start` / `reset_collision` 允许;`stop` / `idle`
   拒绝)。
3. `update()` 第一步 `is_object_alive(host_obj / drive / idler / belt)`
   — 任一 stale → `_alive=False`,manager unregister。
4. **不强行接管 RigidBody**: 只写 `linear_velocity`(Blender 2.7x-4.x)或
   `location`(Blender 5.x),不写 `angular_velocity` / `mass` /
   `rigid_body` 其它字段。没装 `rigid_body` 的物体直接跳过(不抛)。
5. `_clear_blocked_state_only` 命名不可改(`CollisionEngine.set_disabled`
   按方法名遍历)。
6. uv_offset 永远累加(Start/Stop 不重置)。

### 7.3 覆盖验证

- 离线: `pytest tests/test_conveyor.py tests/test_conveyor_runtime.py
  tests/test_conveyor_scene.py tests/test_conveyor_discovery.py
  tests/test_rpc_set_conveyor_running.py` → 96 passed
- 整体: 357 passed / 3 failed(基线 264 passed / 3 failed,新增 96 passed
  且未引入新失败;3 个 pre-existing failure 与本 module 无关 —
  RotateAxis home blocked state 历史问题)
- 覆盖率: `engine.structure_snapshot()["uncovered"]` 必为空
  (4 个部件都在 members 里)

## 8. 典型用法

### 8.1 创建一个 conveyor

```python
import bpy

# 1. host
host = bpy.data.objects.new("Conveyor1", None)
host.empty_display_type = "PLAIN_AXES"
bpy.context.collection.objects.link(host)

# 2. drive / idler 圆柱体
bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=0.5, location=(0, 0, 0))
drive = bpy.context.active_object; drive.name = "Drive"
bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=0.5, location=(4, 0, 0))
idler = bpy.context.active_object; idler.name = "Idler"

# 3. belt 长方体
bpy.ops.mesh.primitive_cube_add(size=1, location=(2, 0, 0))
belt = bpy.context.active_object; belt.name = "Belt"
belt.scale = (4, 0.8, 0.1)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

# 4. 配 cfg
host.conveyor.enabled = True
host.conveyor.drive_roller = drive
host.conveyor.idler_roller = idler
host.conveyor.belt = belt
host.conveyor.target_speed = 200.0   # 200 mm/s
host.conveyor.direction = "forward"
host.conveyor.friction = 1.0

# 5. 放一个 cube 在 belt 上
bpy.ops.mesh.primitive_cube_add(size=0.4, location=(2, 0, 0.2))
cube = bpy.context.active_object; cube.name = "MyCube"
bpy.ops.rigidbody.object_add()       # 装 RigidBody

# 6. 在 Object Properties → Conveyor 面板点 "Refresh"
#    (或运行时调 discovery.discover_and_register)
#    然后把 Running toggle 打开 → 下一 tick cube 开始被推
```

### 8.2 RPC 控制

```python
import socket, json

def call_rpc(req):
    s = socket.socket()
    s.settimeout(2.0)
    s.connect(("127.0.0.1", 9877))
    s.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8"))
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk: break
        buf += chunk
        if b"\n" in buf: break
    s.close()
    return json.loads(buf.partition(b"\n")[0].decode("utf-8"))

# 启动
print(call_rpc({
    "id": 1, "method": "set_conveyor_running",
    "params": {"module_id": "Conveyor1", "running": True},
}))

# 只改速度
print(call_rpc({
    "id": 2, "method": "set_conveyor_running",
    "params": {"module_id": "Conveyor1", "target_speed": 500.0},
}))

# 反向
print(call_rpc({
    "id": 3, "method": "set_conveyor_running",
    "params": {"module_id": "Conveyor1", "direction": -1},
}))

# 停止
print(call_rpc({
    "id": 4, "method": "set_conveyor_running",
    "params": {"module_id": "Conveyor1", "running": False},
}))
```

### 8.3 验收检查清单(线上,blender-mcp `execute_code`)

```python
from MotionSimulation.addon import get_manager, get_collision_engine

mgr = get_manager()
engine = get_collision_engine()

# 1. module 已注册且 snapshot 字段齐全
for m in mgr.all():
    if m.kind == "conveyor":
        snap = m.snapshot()
        assert set(snap) >= {
            "module_id", "kind", "state", "running", "direction",
            "target_speed", "friction", "driven_names", "stop_reason",
        }
        # belt_dir / belt_speed_world / uv_offset / driven_count
        # 已从 state_push 移除(省带宽),面板显示读实例值

# 2. 覆盖率
snap = engine.structure_snapshot()
assert snap["uncovered"] == []
assert "Conveyor1" in snap["groups"]

# 3. 物理驱动(belt + 带 RigidBody 的 cube 场景)
cube = bpy.data.objects["MyCube"]
# 启动后下一 tick 起 cube 沿 belt_dir 方向位移
# 停止后下一 tick cube 不再被覆写
```

## 9. 与其它 module 的关系

| 维度 | Conveyor | LinearAxis | Cylinder | VacuumNozzle | Sensor |
|------|----------|-----------|----------|--------------|--------|
| **kind** | `conveyor` | `linear_axis` | `cylinder` | `vacuum_nozzle` | `sensor` |
| **category** | `axes` | `axes` | `cylinders` | `vacuum_nozzles` | `sensors` |
| **是否动自己** | ❌(驱动外部) | ✅ | ✅ | ❌(reparent) | ❌ |
| **collision bodies** | `()` | `[slider]` | `[work_bar]` | `()` | `()` |
| **state** | idle / running / blocked | idle / homing / moving_*/ blocked | idle / moving_to_*/ blocked | disabled / idle / holding / blocked | active / disabled / blocked |
| **核心动作** | set_running / set_speed / set_direction / set_friction / start | move_to / jog / home / velocity / stop | set_outputs | set_enabled / force_release | (read-only trigger) |
| **驱动方式** | 写 `obj.linear_velocity` 或 `obj.location` | 写 `slider.location` | 写 `work_bar.location` | reparent | 读 sensor AABB |

Conveyor 是**第一个**会修改"自己模块之外物体状态"的 module(LinearAxis /
Cylinder / VacuumNozzle / Sensor 都不这么干)。这是 conveyor 语义上"推动
外部物体"的本质决定的 —— 与 axis 类根本不同。collision 契约仍严格遵守
(`bodies=()`,R1/R2 规则照常工作)。

## 10. 已知 pitfalls(从测试抽取)

| 坑 | 触发 | 防御 |
|---|------|------|
| **discovery 把 `scene` 加到 kwargs 后老 discoverer 不兼容** | `modules/Cylinder/__init__.py` 等老 discoverer `discover(obj, collision_engine=...)` 不接受 `scene=` | `discovery.discover_and_register` 用三层 `try/except TypeError` fallback:`discover(obj, collision_engine=, scene=)` → `discover(obj, scene=)` → `discover(obj, collision_engine=)` → `discover(obj)`(测试 `test_discovery.py` 锁定) |
| **`runtime.py` 不能 `import bpy` 或 `from .component import`** | 离线 pytest 没有 bpy;`from .component` 相对 import 失败 | runtime 模块级 `DIR_FORWARD/DIR_REVERSE` 常量重新定义(不依赖 component);runtime 顶层的 `from ... import collision` 是惰性 / 容错的 |
| **两 roller 同位置 → belt_dir 无定义** | artist 把 drive / idler 重合 | discovery 校验 `drive is idler` 跳过;runtime `__init__` raise `ValueError` |
| **两 roller 是同一个 Object** | artist 误配 | discovery 校验并打印 `must be distinct objects`,跳过 |
| **物体没 rigid_body → 不被驱动** | 静态装饰物体放在 belt 上 | `_collect_free_recursive` 过滤 `rigid_body is None`,不抛 |
| **物体挂在其它 module 子树里** | 比如把 cube parent 到 LinearAxis slider 下 | `_collect_skip_set` 包含其它 module 的 host_obj,`_collect_free_recursive` 遇到则整棵 subtree 跳过 |
| **物体太高(>5cm 在 belt 上方)→ 不算接触** | 物体飞起来 | 高度容差 `DEFAULT_HEIGHT_TOLERANCE_BU = 0.05`,可调 |
| **uv_offset 永远累加** | artist 想"调速度 = 重置滚动态" | 设计上是未来 work;本次未实现 `reset_uv_offset` 字段 |
| **被驱动物体挂着 RigidBody 但 `kinematic=True`** | artist 把它当静态装饰用了 | kinematic=True 时 physics 不响应 location 写入,物体保持不动 —— 这是 Blender 自身行为,不是 conveyor 的 bug |
| **stale datablock**(host / 任一 roller 被删) | artist 编辑场景时删了部件 | `update()` 第一步 `is_object_alive` 校验 → `_alive=False`,manager unregister |
| **场景树遍历只走顶层 root** | 同一 root 下的所有 objects 都被当作自由对象 | 这是 by design —— 子树已在 `skip_set` 里被跳;艺术家若想隐藏某对象,把它的 `hide_viewport` 设上即可 |

## 11. 实现参考

| 想看什么 | 优先读 |
|---------|--------|
| 模块骨架 / PropertyGroup | `modules/Conveyor/component.py` |
| Runtime 核心 | `modules/Conveyor/runtime.py`(ContactorModule 同 LinearAxis / Cylinder 的风格,base + state machine + collision_structure) |
| Discovery 契约 | `modules/Conveyor/discovery.py`(同 Cylinder / Sensor 模块) |
| UI 面板 | `modules/Conveyor/ui.py`(Object Properties 面板 + Refresh / Apply operator) |
| N-Panel 集成 | `modules/dev_panel.py::_draw_conveyor_body` + `MS_PT_dev_conveyor` |
| RPC 顶层 method | `rpc/server.py::_handle_set_conveyor_running` + `_handle_message` 路由 |
| RPC doc | `doc/RPC.md` §4.10(set_conveyor_running) + §5.2(Conveyor snapshot) + §5.3(Conveyor state) |
| 测试 | `tests/test_conveyor.py` + `tests/test_conveyor_runtime.py` + `tests/test_conveyor_scene.py` + `tests/test_conveyor_discovery.py` + `tests/test_rpc_set_conveyor_running.py` |

---

## 附录:基线对照表(plan → 实际)

| 字段 | plan § 假设 | 实际 | 备注 |
|------|-----------|------|------|
| 物理模型 | 直接写 linear_velocity | 直接写 linear_velocity(Blender 2.7x-4.x)→ 退到 location 累加(Blender 5.x) | 离线测试用 mock `linear_velocity` setter;线上 Blender 5.x 走 location 累加 |
| bucket 桶 | 共享 axes(`category="axes"`) | ✅ 一致 | `data["axes"]` 同桶 |
| 主动 / 从动 | 两 pointer + direction | ✅ 一致 | `drive_roller` / `idler_roller` + `direction` |
| friction 语义 | 1.0 完全抓地, 0.0 完全打滑 | ✅ 一致 | 沿 `belt_dir` 一维 lerp |
| 默认值 | target_speed=50, direction=forward, friction=1.0 | ✅ 一致 | |
| 接触容差 | 高度 5cm,横向 2cm | ✅ 一致 | `DEFAULT_HEIGHT_TOLERANCE_BU = 0.05`,`DEFAULT_LATERAL_TOLERANCE_BU = 0.02` |
| uv_offset 累加 | 永远累加 | ✅ 一致 | |
| RPC method 名 | `set_conveyor_running` | ✅ 一致 | |
| 不参与联锁 | conveyor 进 BLOCKED 不影响 axis | ✅ 一致 | 各 module 各自响应全局 marker |
| 新增测试 | 96 个新测试 | ✅ 96 个全部通过 | |