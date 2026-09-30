# Collision 工作手册

> 本文档讲当前代码 `modules/components/collision.py` 的工作方式：
> 哪些对象被识别为"轴成员"、BVH 缓存怎么构建、
> `check_slider_collision` 一次检测的完整流程、跨 axis 排除 / 嵌套
> axis 屏蔽、以及触发后状态机如何反应 + 美术端怎么手动 disable。

## 1. 用到的对象

碰撞检测依赖三类 Blender 对象：

| 类别 | 含义 | 例子 |
| --- | --- | --- |
| **axis body** | 每个 axis 正在驱动的可动物体 | `LinearAxis1.Slider`、`LinearAxisY.Slider.2`、`RotateAxis2.Rotator` |
| **axis-owned object** | 同一 axis 的所有子物体（slider + rail + sensors + shim + 子树里所有 mesh） | 由 `axis_object_names(id)` 返回 |
| **non-axis obstacle** | scene 里不属于任何 axis 的 MESH 物体 | `立方体`、独立 baseplate 等 |

判断一个对象是否属于 axis 的依据是 **`SimulationManager.axis_object_names(id)`** —— 它调用 `_collect_axis_object_names(module)`，从 module 的 `host_obj`、`slider`、`rail`、`rotator`、`center`、`shim` 字段出发递归遍历子树，把所有命名收集进集合 `_axis_object_names_by_id[axis_id]`。union 之后存在 `_axis_object_names`，`all_axis_object_names()` 也可以扁平取回。

## 2. 嵌套 axis 的 fence（commit `6a59a27`）

如果一个 artist 把 Y-axis host 挂到 X-axis slider 下（"Y 跟着 X 动"），`_walk_subtree(LinearAxis1)` 沿 Slider → LinearAxisY → Slider.2 → ... 会把 LinearAxisY 整个子树都收集进 LinearAxis1 的 owned set。Collision engine 看到 LinearAxis1.BVH 跟 LinearAxisY.BVH 重叠就报 hit —— 但 artist 的语义是"同一载体"，不该报。

**修法**：`SimulationManager._collect_axis_object_names(module, stop_names=...)` 接受 `stop_names` 参数；`_walk_subtree(obj, visited, stop_names)` 遇到 stop_names 里的名字时**整个子树不递归**，不只是跳过 name。

`register_module` 每次注册时调 `_invalidate_owned_names()` 把 cache 标 dirty。`axis_object_names(id)` / `all_axis_object_names()` 第一次读时调 `_recompute_owned_names()`，用**当前完整的 registered host 集合**算 stop_names。

这样无论 axis 注册顺序怎样、scene tree 怎么嵌套，每个 axis 的 owned 集都正确地不越过其他 axis 的 host 子树。

## 3. Parent-child axis 的 ancestry 屏蔽（commit `0691f5a`）

即使 fence 让 `axis_object_names` 不互相污染，**LinearAxis1.Slider（body）跟 LinearAxisY.BVH（另一个 axis 的合并 BVH）** 的 BVH overlap 在物理上还是可能存在的（比如 Slider 跟 LinearAxisY 的 baseplate 真的相交）。但 artist 的语义：LinearAxisY 跟着 Slider 走，"同一载体的运动"，不当 collision 处理。

**修法**：`CollisionEngine._axes_are_related(a, b)` 检查两个 axis 的 `host_obj` 在 scene tree 里是否有 ancestor-descendant 关系。`check_slider_collision` 在跨 axis 循环里跳过 related pairs。

`_axis_host_map: Dict[str, host_obj]` 在每次 `_rebuild_obstacle_cache` 时刷新，源自 `manager.all()`。`_axes_are_related` 用 `seen` set 防 cycle，每次 walk 上下两个方向最多 O(d) 步。

## 4. BVH 缓存

`CollisionEngine` 维护两个缓存 + 一个 host_map：

| 缓存 | 内容 | 何时 rebuild |
| --- | --- | --- |
| `_axis_bvhs: Dict[str, BVHTree]` | 跨 axis 的合并 BVH，每个 axis 一棵 | `rebuild_if_due`（每 30 tick 一次）或 `force_rebuild` |
| `_obstacle_cache: Dict[str, BVHTree]` | 不属于任何 axis 的单个 mesh 的 BVH | 同上 |
| `_axis_host_map: Dict[str, host_obj]` | axis_id → host_obj，给 ancestry check 用 | 跟 cache 同步刷新 |

构建流程（`_rebuild_obstacle_cache`）：

1. 通过 `axis_names_provider` 把 union 拉到 `new_flat`（仅保留下来供旧 API 兼容）。
2. 通过 `per_axis_provider(id)` 对每个 registered axis 收集 names，**用 `_walk_subtree` 并 fence 出其他 axis 的 host 子树**。对应的 scene 对象**合并**进一棵 BVHTree（`_build_combined_bvh`）。
3. scene 里所有 MESH 中，`is_obstacle_candidate(obj, set())` 通过的（即不是 None / 非 MESH / 无 polygon / hidden），且 name **不在** 任何 axis-owned 集合里 → 单个 mesh 各自一棵 BVHTree（`_build_bvh`）。
4. 替换 `_axis_bvhs`、`_obstacle_cache`、`_axis_host_map`（最后一个从 `manager.all()` 拿 host_obj）。

`_build_bvh` 和 `_build_combined_bvh` **关键步骤**：

- `obj.evaluated_get(depsgraph)` 拿到 evaluated mesh（含 modifier / shape key）
- 取 `obj.matrix_world` 把顶点 `mw @ v.co` 变到世界系
- **fan-triangulate 每个多边形**（Blender 5.x 的 `BVHTree.FromPolygons` 不再自动 tessellate quad / n-gon；`all_triangles=True` 已失效；commit `b54e49b`）
- `BVHTree.FromPolygons(verts, tris, all_triangles=False, epsilon=0.0)`

`_fan_triangulate` 用 fan triangulation（每个 N-gon 拆成 N−2 个三角形共享第一个顶点），并通过 `poly.vertices` 而非 `len(poly)` 适配 Blender 5.x 的 MeshPolygon API（commit `b54e49b`）。

## 5. 查询：`check_slider_collision`

`CollisionEngine.check_slider_collision(slider_obj, depsgraph, axis_id=None)` 一次完整流程：

```
输入：slider_obj（axis body 的 Blender 物体），depsgraph，axis_id（被检查 axis 的 id）。
```

1. **`_enabled` 关闭** → 直接 `False`（见 §6）。
2. `slider_obj is None` 或 `depsgraph is None` → `False`。
3. `_build_bvh(slider_obj, depsgraph)` 构造 slider 自己的世界系 BVH → 失败 → `False`。
4. **跨 axis 循环**：遍历 `_axis_bvhs.items()`：
   - `other_axis_id == axis_id` → 跳过（同一 axis 内部不算）。
   - **`_axes_are_related(axis_id, other_axis_id)`** → 跳过（parent-child axis pair，artist 语义为"同载体"）。
   - `bvh_overlap_exists(slider_bvh, axis_bvh)`（即 `axis_bvh.overlap(slider_bvh)`）→ 命中，记录 `last_collision = {"axis_id": other_axis_id, "obstacle_name": "axis:<id>", "slider_name": slider_obj.name}` → `True`。
5. **非 axis 障碍循环**：遍历 `_obstacle_cache.items()`：
   - `bvh_overlap_exists(slider_bvh, obstacle_bvh)` → 命中，记录 `last_collision = {"axis_id": passed_in_axis_id, "obstacle_name": <name>, "slider_name": slider_obj.name}` → `True`。
6. 都不命中 → `False`。

注意：
- 一次调用只对 **单个 axis body** 做一次检测。Slider / Rotator / TriggerShim 各自被显式传入。
- `last_collision` **只在命中时写入**。它不会被 `clear_collision` 自动清零，需要用户在面板里手动点 "Reset Flag" 才会重置。

## 6. 美术端 Disable Collision（commit `54989bc`）

有些 artist 只想让 rig "停一下" 检查 scene，不想每个 tick 都被 collision block 反复困住。MotionSimulation Dev panel 加了 "Disable Collision" 按钮：

```python
class CollisionEngine:
    def __init__(self, scene):
        ...
        self._enabled: bool = True   # 默认开启

    def set_disabled(self, disabled: bool) -> bool:
        new_state = not bool(disabled)
        changed = new_state != bool(self._enabled)
        self._enabled = new_state
        if new_state:
            # Re-enable: clear any stale flag so the next tick
            # starts from a known-clean state.
            self.clear_collision()
        return changed

    def read_marker(self) -> bool:
        if not self._enabled:
            return False
        ...

    def mark_collision(self) -> None:
        if not self._enabled:
            return
        ...
```

Dev panel 在 N-panel ▸ MotionSimulation tab ▸ Global controls 段：
- "Collision detection: ENABLED / DISABLED" label（带 CHECKMARK / CANCEL 图标）
- **[Disable]** 按钮（启用时显示）→ `set_disabled(True)` → 关闭
- **[Enable]** 按钮（关闭时显示）→ `set_disabled(False)` → 自动 clear_collision 后开启
- **[Reset Flag]** 按钮 → 单独清 stale scene marker（不影响 enable 状态）

行为：
- **关闭时**：`mark_collision` no-op，`read_marker` 返回 False。`check_slider_collision` 仍跑 BVH 检测、仍更新 `last_collision`（Dev panel 还能看到"什么会撞"），但 axis 的 `_check_transitions` 看到 `read_marker() == False` 就**不进 STATE_BLOCKED**。下一 tick 释放所有 BLOCKED 的 axis。
- **开启时**：自动 `clear_collision` 清 stale marker（避免之前没处理的 marker 突然生效）。

## 7. 触发后的状态机

每个 axis 的 `update(dt)` 流程（LinearAxis / RotateAxis 镜像）：

1. **scene-level marker 检查**（在 axis.update 开头）：
   ```python
   if engine.read_marker():    # engine._enabled 关闭 → 返回 False
       if state != STATE_BLOCKED:
           self._enter_blocked_state()    # state = "blocked"; stop_reason = "collision"
       self._consume_cmd_blocked()
       self._write_state_props()
       return                            # 完全跳过本帧其它逻辑
   ```
2. 正常流程：`_consume_cmd → _update_sensors → _update_slider/rotator`
3. `_collision_engine.check_slider_collision(...)`：
   - 命中（但 engine._enabled=True 才写 marker）→ `_collision_engine.mark_collision()` 写 scene-level marker + `_enter_blocked_state()` 让本 axis 也变 BLOCKED
   - 不命中 → 正常状态
4. 持续到下一帧 —— 一旦 `mark_collision()` 把 scene marker 写成 True，**所有** 其他 axis 在 tick 开头看到 `engine.read_marker() == True` 也会被 `enter_blocked_state()` 锁住。

`reset_collision()` 是出口：
- 调用 `engine.clear_collision()`（清 scene-level marker、`last_collision = None`）
- 如果本 axis 处于 BLOCKED：调 `slider.start_idle()`、`state = IDLE`、`_stop_reason = ""`，本帧释放。

## 8. scene-level marker

`scene["motion_simulation_collision"]` 是 boolean custom property。

| 写入 | 读取 | 用途 |
| --- | --- | --- |
| `mark_collision()` 写 True（仅当 `_enabled=True`）| `read_marker()` 读（仅当 `_enabled=True`）| 任意 axis 触发后立刻传播到所有 axis |
| `clear_collision()` 写 False | 每个 axis tick 开头读，决定是否锁住 | 手动 reset 后所有 axis 自动恢复 |
| `set_disabled(True)` 不改 scene，只置位 `_enabled` | `read_marker()` 在 `_enabled=False` 时**永远返回 False** | artist "一键停车" |
| `set_disabled(False)` 自动 `clear_collision()` | `_enabled` 恢复 True，重新看 scene | re-enable 时清 stale |

外部观察者（MCP `execute_code`、RPC subscriber、Dev panel）通过 `scene.get("motion_simulation_collision", None)` 读到当前全局状态。

## 9. 调用时序

每帧的 tick：

```
SimulationManager.update(dt)
  for module in manager.all():
    module.update(dt)
      ├── 如果 read_marker() 返回 True → STATE_BLOCKED 短路（仅当 _enabled=True）
      ├── _consume_cmd → _update_sensors → _update_slider/rotator
      ├── collision_engine.rebuild_if_due(depsgraph)   ← 每 30 tick 重建一次缓存
      │   └── _rebuild_obstacle_cache(depsgraph)
      │       ├── 对每个 axis 收集 owned set（fence 排除其他 host 子树）
      │       ├── 合并成 axis BVH
      │       ├── 收集 non-axis obstacle 单 mesh BVH
      │       └── 刷新 _axis_host_map
      ├── collision_engine.check_slider_collision(body, depsgraph, axis_id)
      │   ├── engine._enabled False → False
      │   ├── 跨 axis 循环：跳过 self / 跳过 ancestry-related / bvh_overlap? → True
      │   └── obstacle 循环：bvh_overlap? → True
      └── 触发 → mark_collision + _enter_blocked_state
```

默认 `rebuild_every_n_ticks = 30`。如果是离线测试或者要立刻看到新加的物体，可以用 `engine.force_rebuild(depsgraph)` 手动触发一次。

## 10. 调试入口

**MCP 客户端**（端口 9876）：

```python
import sys, importlib.util, types, os
ADDON_DIR = r"C:\Users\boshi.tang\AppData\Roaming\Blender Foundation\Blender\5.1\scripts\addons\MotionSimulation"
for k in list(sys.modules):
    if k.startswith("MotionSimulation"):
        del sys.modules[k]
pkg = types.ModuleType("MotionSimulation")
pkg.__path__ = [ADDON_DIR]
sys.modules["MotionSimulation"] = pkg
def load_as(path, name):
    full = "MotionSimulation." + name
    spec = importlib.util.spec_from_file_location(full, os.path.join(ADDON_DIR, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod
# ... (load framework, simulation_manager, discovery, modules.* etc.)
addon = load_as("addon.py", "addon")
mgr = addon.get_manager()
eng = mgr.get_collision_engine()
print("engine enabled:", eng.is_enabled)
print("scene marker:", bpy.context.scene.get("motion_simulation_collision"))
print("obstacle_count:", eng.obstacle_count, "axis_bvh_count:", eng.axis_bvh_count)
for m in mgr.all():
    body = m.slider.obj if hasattr(m, "slider") and m.slider else (m.rotator.obj if hasattr(m, "rotator") and m.rotator else None)
    if body is None: continue
    hit = eng.check_slider_collision(body, bpy.context.evaluated_depsgraph_get(), axis_id=m.axis_id)
    print(f"  {m.module_id}: hit={hit}")
```

**Blender 控制台**（更轻量）：

```python
import sys
mgr = sys.modules["MotionSimulation.addon"].get_manager()
eng = mgr.get_collision_engine()
print("enabled:", eng.is_enabled, "marker:", bpy.context.scene.get("motion_simulation_collision"))
for m in mgr.all():
    print(f"  {m.module_id}: state={m.state!r} stop_reason={m._stop_reason!r}")
```

**Dev panel（artist 主用路径）**：
- 3D 视图 ▸ N 面板 ▸ MotionSimulation tab
- Global controls 段：[Refresh] / [Disable | Enable] / [Reset Flag]
- Collision status 段：scene flag、obstacles / excluded 数量、last_collision 详情
- Linear Axes / Rotate Axes 子面板：每个 axis 的 state / position / target / velocity / sensors ON

**关键 API**：
- `engine.force_rebuild(depsgraph)` —— 立刻重建缓存
- `engine.check_slider_collision(body, depsgraph, axis_id)` —— 一次检测
- `engine.set_disabled(True/False)` —— 开关 collision
- `engine.clear_collision()` —— 单独清 marker
- `engine.last_collision` —— 最近一次命中记录
- `manager.axis_object_names(axis_id)` —— 某个 axis 的 owned 集
- `manager.all_axis_object_names()` —— 全部 axis owned 的 union

## 11. 相关文件

- `modules/components/collision.py` —— CollisionEngine 实现
- `simulation_manager.py` —— `_collect_axis_object_names`、`axis_object_names`、`_walk_subtree`
- `modules/LinearAxix/axis.py` —— LinearAxis `update()` 里的 collision 调用点
- `modules/RotateAxis/rotate_axis.py` —— RotateAxisRuntime 同
- `modules/dev_panel.py` —— Dev panel 渲染 + Disable/Reset 按钮
- `tests/test_collision.py` —— 离线纯算 helper unit tests
- `tests/test_axis_fence.py` —— fence / ancestry unit tests
- `tests/test_collision_disable.py` —— enable / disable toggle unit tests

## 12. 刚体 vs 刚体豁免（2b，`kind="rigid_contact"`）

搬运场景里工件与工件的接触（conveyor 上的工件撞 work bar 上的工件）是**工艺接触**，
不该触发碰撞停车。判定规则（`modules/components/collision.py`）：

- 命中两侧**都是刚体**（运动体 + 被撞组合的全部实际重叠成员都带 `rigid_body`）→
  命中降级为 `kind="rigid_contact"`、`exempt=True`：**不写 scene marker、不进 BLOCKED、
  不通知模块**；命中记录照留（`hit_for` / `last_collision` / Dev panel / RPC 可见）。
- 任一侧不是刚体（机器件如 PushBar、皮带、滚筒等无 `rigid_body`）→ **照常报警**。
  这是**严格**口径（两侧刚体才豁免）。
- 重叠成员中混有非刚体 → 照常报警（保守）。
- 过滤点：`step()`（拉模式）与 `module_self_check()`（兼容路径）内置；
`is_blocked()` 不把豁免命中算作肇事。
- 识别函数：`collision_structure.is_rigid_body_object(obj)`（duck-typing，离线 mock 可用）。

与 conveyor 的分工：工件间接触的**处理**（顶住停在接触处、让开续走）在
`ConveyorModule` 的位置推动逻辑里（1a，见 `doc/CONVEYOR.md` §3.5）；
碰撞引擎只负责"不因此报警"。