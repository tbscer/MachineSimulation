# -*- coding: utf-8 -*-
# 执行计划：Conveyor 回退位置驱动 + 刚体接触不报警 + 模块注册修复

文档性质：**执行计划（已获用户确认）**。决策来自 2024 会话讨论：
**1a**（顶住停在接触处、每 tick 重试）、**2b**（两侧都是刚体才豁免报警）、
**一并修复 live manager 模块注册数为 0 的问题**。

---

## 0. 背景与结论（勘察已完成）

### 场景现状（MCP 实测）
- `Conveyor1`(EMPTY host，cfg 齐) + `Belt_Top` / `Belt_Top.002` / `Belt_Bottom`
  （简化循环皮带，PASSIVE+kinematic BOX 刚体）+ `DriveRoller` / `DrivenRoller`。
- `TestBoard`：皮带上的工件，ACTIVE 刚体（质量≈1e9，已跑飞到 z=-18603）。
- `Cylinder1`(EMPTY host) + `PushBar`(work_bar) + `立方体.003`（work bar 上的工件，
  **当前无 rigid_body**）+ `Stopper`(PASSIVE 刚体挡块)。

### 代码勘察结论
1. **两侧版本分歧**：仅 `modules/Conveyor/runtime.py` 不一致——
   Blender 侧 40181 字节（= 位置计算旧路径，"从前"的行为），沙箱侧 43046 字节
   （= 旧路径 + Phase 2 物理驱动增量 `_apply_belt_motion` / `_wrap_belt`）。
   **驱动回退 = 以 Blender 侧 40181 版为基线，不含 Phase 2 增量**。
2. **碰撞检测语义**：`CollisionEngine` 只检测"运动体(bodies) vs 其它组合的 BVH"。
   本场景唯一运动体是 `PushBar`；两个工件互撞**引擎本来就不检测**，工件间接触
   必须由 conveyor 驱动逻辑处理（这正是"放到 conveyor 的驱动逻辑中"）。
3. **模块注册为 0**：`Conveyor1`/`Cylinder1` 带 `_conveyor_discovered` /
   `_cylinder_discovered` 标志但 manager 名册为空。机制上"标志存在 + 未注册"
   只可能来自：`clear_discovered_flags` 或 `discover_and_register` 中途抛异常
   （两者对 `is_host` / `clear_flag` / `discover` 的异常无逐项防护），
   或注册后 manager 被清空。执行时先运行时定位根因再修。

## 1. 决策（用户确认）
| 项 | 决策 |
|---|---|
| 推动语义 | **1a**：驱动的工件顶到别的刚体 → 停在接触处（不穿透），每 tick 重试，对面让开后自动续走 |
| 豁免范围 | **2b**：命中两侧**都是**刚体（带 `rigid_body`）才豁免报警；`PushBar`（无刚体）蹭到 `TestBoard` 仍会报警（用户知情选择） |
| 驱动方式 | 回退位置计算（`_scan_objects_on_belt` + `_apply_velocity` 写 location），不走 Bullet/摩擦；**不碰 frame 逻辑** |
| 注册问题 | 一并修复 |

## 2. 实施步骤

### Step 0｜修复模块注册（前置）
1. MCP 运行时定位：逐步执行 `clear_discovered_flags` / `discover_and_register`，
   捕获异常与各 discoverer 行为，确认根因。
2. 修复（按根因）：大概率为 `discovery.py` 的两个遍历循环缺少逐项 try 防护 +
   底层 bug；修复后 addon 重载 → 验证 `manager.all()` 注册数 ≥ 3
   （Conveyor1 / Cylinder1 / ApproachSensor2）、`structure_snapshot()`
   中 Cylinder `bodies=["PushBar"]`、`uncovered == []`。
3. 顺手处理场景件：`立方体.003` 是否补 rigid_body（工件识别依据）、
   `TestBoard` 复位到皮带上。

### Step 1+2｜Conveyor 驱动回退 + 1a 接触处理（modules/Conveyor/runtime.py）
- 基线 = Blender 侧 40181 旧版（位置计算路径），沙箱同步成最终版（删 Phase 2 增量）。
- `_apply_velocity`/驱动循环加**1a 位置预判**：
  - 候选障碍 = 其它带 `rigid_body` 的 MESH，排除：自身、自身父子链、
    conveyor 自己的 belt/roller 成员（支撑面不算阻挡）。
  - 先试全额位移；若目标 AABB 与候选障碍重叠则二分收缩到位移前刚好
    不重叠（"停在接触处"），本 tick 就到此为止；下个 tick 继续尝试
    （对面让开 → 自动续走）。
- 皮带方块不移动；`belt_uv_offset` 视觉滚动保留。
- **不动** `simulation_manager.py` 的 tick / frame / 物理时钟逻辑。

### Step 3｜collision 2b 豁免（modules/components/collision.py + collision_structure.py）
- 新增纯 duck-typing 判定 `is_rigid_body_object(obj)`（有 `rigid_body` 即算刚体）。
- 命中判定处：body 与被撞组合的**实际重叠成员**逐个求交（把现有
  `_refine_structure_hit` 泛化为"全部重叠成员"）：
  - body 有刚体 **且** 全部重叠成员有刚体 → 命中记录 `kind="rigid_contact"`，
    **不写 scene marker、不 BLOCKED、不通知模块**；记录照留
    （`hit_for` / `last_collision` / Dev panel / RPC state_push 可见）。
  - 任一侧不是刚体 → 照常报警（机器 vs 机器保护不变）。
- 只改 collision 组件（符合"新判定逻辑只能落在 collision"法则）；
  兼容面（`check_slider_collision` 等方法名与语义）保持不变。

### Step 4｜测试 + 文档 + 上线验证
- 沙箱 pytest：新增用例（1a 阻挡/续走、2b 豁免/仍报警矩阵）；
  基线不得新增失败（skill 记录基线 516 passed / 13 failed）。
- 文档同步：`doc/COLLISION_GUIDE.md` §0 系列、`.pi/skills/collision-rule/SKILL.md`
  （新 kind `rigid_contact`、2b 规则、排查表）、`doc/CONVEYOR.md`（驱动回退说明）。
- 线上验收（MCP）：
  1. 启动 conveyor → `TestBoard` 被位置驱动前进（摩擦/物理不参与）；
  2. 顶到 `立方体.003` → 停在接触处、不穿透、marker 不置位；
  3. work bar 把方块挪走 → `TestBoard` 自动续走；
  4. 反向：`PushBar` 压到皮带/滚筒组 → 仍报警（BLOCKED）。
- **写入纪律（CLAUDE.md）**：插件源码沙箱打补丁（断言-替换-语法校验-回读）→
  base64 推 Blender 落盘 → 双侧 md5 一致 → disable/purge/enable 重载验证；
  测试与文档只写沙箱侧。

## 3. 风险与备注
- 2b 严格口径下，`PushBar` 本体若与 `TestBoard` 几何接触，报警仍会出现
  （用户知情）。若实测确有干扰，一行放宽为 2a（任一侧是刚体即豁免）。
- `TestBoard` 质量 ≈1e9 且已跑飞：验收前复位；若 Bullet 在播放时仍会推进
  ACTIVE 刚体，位置写入与物理仿真可能轻微打架（旧路径固有行为，接受）。
- `立方体.003` 目前没有 rigid_body：若不补，它不被识别为刚体，
  1a 阻挡与 2b 豁免都不覆盖它 → 计划默认补上（ACTIVE）。

## 4. 验收清单
- [ ] `manager.all()` 注册数正确，`uncovered == []`
- [ ] conveyor 位置驱动工作，皮带方块不移动，frame 逻辑无改动
- [ ] 工件顶住停在接触处、让开续走（1a）
- [ ] 双刚体命中 → `kind="rigid_contact"`、无 marker、无 BLOCKED（2b）
- [ ] 机器 vs 机器 → 仍报警
- [ ] pytest 无新增失败；文档与 skill 同步；双侧文件 md5 一致