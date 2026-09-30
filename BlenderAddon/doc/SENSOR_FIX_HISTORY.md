# U 型 Sensor 工作手册

> 本文档讲当前代码状态下，`UTypeSensor` 如何判断 mesh 是否像 U 型
> 传感器，以及运行时如何检测遮挡物是否进入槽内。

## 1. 入口：`UTypeSensor._detect()`

`modules/LinearAxix/axis.py:_update_sensors()` 每帧调用：

```python
source = self.shim.obj if self.shim is not None else self.slider.obj
for s in self.sensors:
    s.update(source)            # → BaseSensor.update → UTypeSensor._detect
```

`BaseSensor.update` 把 `_detect` 的返回值与缓存 `_is_triggered` 对比，
**变化时**才写 `obj["is_triggered"]` 并返回 True（"状态变化"事件）。

`UTypeSensor._detect()` 调用顺序：

```
_detect(source_obj, depsgraph):
├─ rebuild_geometry() 若 geometry is None
├─ if geometry.is_invalid:                       return False       ← 提前拒绝
├─ if sensor_max_a − sensor_min_a ≤ EPSILON:     return False       ← 退化保护
├─ 算 src AABB（在 sensor 局部坐标系下）
├─ depth gate: src_min[a] ≥ sensor_min[a] − EPS
│                AND src_max[a] ≤ sensor_max[a] + EPS                ← 深度门
└─ _opening_area_containment(aabb):                                      ← 截面门
```

只有 depth 和截面门都通过才返回 True。

## 2. 几何分析：`UTypeGeometryAnalyzer.analyze()`

只跑一次（在 `__init__` 和 `rebuild_geometry` 里）。流程：

```
analyze(obj):
├─ vertices = _get_local_vertices(obj)
├─ if len(vertices) < 4:  return _bound_box_fallback(obj)         ← 无 mesh 走兜底
├─ min_v, max_v = _bounds(vertices)
├─ axis_scores = [
│     _estimate_opening_score(vertices, ax, min_v, max_v)
│     for ax in (0, 1, 2)
│   ]
├─ opening_axis = max(range(3), key=lambda ax: axis_scores[ax])
├─ opening_sign = _detect_opening_sign(vertices, opening_axis, min_v, max_v)
├─ slot_width_a, slot_width_b = _calculate_slot_widths(
│       vertices, opening_axis, opening_sign, min_v, max_v)
└─ return _validate_extents(UTypeGeometry(...))
```

返回值 `UTypeGeometry`：

| 字段 | 含义 |
| --- | --- |
| `opening_axis` | 0 (X) / 1 (Y) / 2 (Z) |
| `opening_sign` | +1 / -1：开口朝哪一端 |
| `sensor_min_local` / `sensor_max_local` | mesh 在 sensor 局部坐标系下的 AABB |
| `slot_width_a` / `slot_width_b` | U 槽两个垂直方向上的"两臂间空隙"宽度 |
| `detection_center_local` | 检测平面中心 |
| `is_invalid` | 几何无效（占位 mesh / 退化）→ `_detect` 直接 False |

### 2.1 选开口轴：`_estimate_opening_score`

替换了早期基于 2D 投影 occupancy 的算法。现在用 **密度不对称度**：

```python
size = max_v[axis] − min_v[axis]
low_count  = sum(1 for v in vertices if v[axis] ≤ min_v[axis] + size*0.20)
high_count = sum(1 for v in vertices if v[axis] ≥ max_v[axis] − size*0.20)
return abs(high_count − low_count)        # ← 分数越大越像开口轴
```

`analyze` 用 `max(...)` 而不是 `min(...)` 选轴——选的是"两端 vertex 数差别最大"的方向。真 ASCII U 槽两个方向：封闭底端 vertex 多，开口端只有 4 个外周 vertex；不对称度最大者就是开口轴。

### 2.2 判开口朝向：`_detect_opening_sign`

```python
if low_count < high_count:    return -1   # 开口朝低端
return +1                       # 开口朝高端
```

搭配 2.1 算出的 `opening_axis`，得到一条局部的开口法向量 `opening_normal_local`，赋给 `DetectionVolume`。

### 2.3 实测槽宽：`_calculate_slot_widths`

```python
# 1. 在开口端 sample_pos = 沿开口轴距该端 5% 处，厚度 10%
near = [v for v in vertices if |v[axis] − sample_pos| ≤ slice_thickness]

# 2. 用 BB 跨度减去薄层顶点的两个垂直方向跨度
slot_a0 = max(0.0, bb_extent_a0 − (slice_max_a0 − slice_min_a0))
slot_a1 = max(0.0, bb_extent_a1 − (slice_max_a1 − slice_min_a1))

# 3. 退化保险：两侧都坍缩到 0 时退回 half-extent 保守值
if slot_a0 ≤ EPSILON and slot_a1 ≤ EPSILON:
    return (bb_extent_a0 × 0.5, bb_extent_a1 × 0.5)
```

退化触发条件：24-vertex 真 ASCII U 槽的开口端 4 个外周 vertex 在 X/Y 方向上的跨距正好覆盖整个 BB 跨度——pure-vertex 算法看不到"凹"。回退到 `bb_extent × 0.5` 给出有意义的截面门宽度。

### 2.4 fallback：`_bound_box_fallback`

`vertices < 4`（mock / empty / 不可评估的 mesh）走这个路径：
- 找最长局部轴当开口轴，`opening_sign=+1`
- 槽宽 = `bb_extent × 0.5`（保守）
- 之后仍走 `_validate_extents`

不可信，但有 `_validate_extents` 兜底。

### 2.5 `_validate_extents` —— 几何有效性护栏

三道护栏按顺序，任一命中即输出 `is_invalid=True` 的退化几何（开口轴 BB 跨度置零，让 `_detect` 的退化保护命中）：

| # | 条件 | 阈值 | 命中场景 |
| --- | --- | --- | --- |
| 1 | 任一轴 BB 跨度 ≤ MIN_VALID_SENSOR_EXTENT | 5 cm | micro cube / 空 / 几 mm 占位 |
| 2 | 三轴 BB `max / min` ≤ ASPECT_RATIO_MIN | 1.5 | 接近正方体 / 14cm cube 占位 |
| 3 | **已撤掉**：实测 slot ≤ MIN_VALID_SLOT_WIDTH (5 mm) | — | 真 ASCII U 槽 24 vertex 也会触发，故撤掉 |

`rebuild_geometry()` 末尾若 `is_invalid=True` 调用 `force_state(False)`，
把 `obj["is_triggered"]` 的 stale True 清掉。

## 3. 运行时检测：`_detect` 两层门

### 3.1 深度门（沿开口轴）

```
src_min[a] ≥ sensor_min[a] − EPS
        AND
src_max[a] ≤ sensor_max[a] + EPS
```

相当于"src 在 sensor 沿开口轴方向的 AABB 内**完全包含**"。任何伸出即 False。这一道同时蕴含方向：src 在传感器封闭背面之后或骑跨开口边沿都被拒。

### 3.2 截面门（两个垂直轴）：`_opening_area_containment`

```
for slot_axis, slot_width in zip(other_axes, (slot_width_a, slot_width_b)):
    if slot_width ≤ EPSILON:
        continue                              ← 跳过，不用拒绝（退化保护已 catch）
    ctr  = detection_center_local[slot_axis]
    half = slot_width × 0.5
    src_min[slot_axis] ≥ ctr − half − EPS
        AND
    src_max[slot_axis] ≤ ctr + half + EPS
```

**完全包含**两个方向上 `detection_center ± half` 的区间。`slot_width ≤ EPSILON` 时跳过而不是拒绝——退化场景已经在前面被清掉了，这一步不该误判。

## 4. src AABB 怎么算

`_get_source_local_aabb(source_obj, depsgraph)`：

1. `world_corners = get_object_world_corners(source_obj)` —— 8 个世界包围盒角点
2. `sensor_inverse = self.obj.matrix_world.inverted()`
3. `local_corners = [sensor_inverse @ p for p in world_corners]`
4. 在 sensor 局部坐标系下取这 8 个点的 AABB

**`source_obj` 是 `LinearAxis._update_sensors` 提供的 shim / slider object**，不是滑块可视化 mesh。Shim 是给 sensor 用的干净 AABB 源，比整个 slider 几何小，能更精确地对齐到 sensor U 槽中。

## 5. 失败模式速查

| 现象 | 可能原因 |
| --- | --- |
| 假 cube 14cm 占位仍 trigger | reload 没生效，Blender 内存里仍是旧 `_detect` |
| 真 U 槽永远 False | `_calculate_slot_widths` 退化分支未触发（修过）；或者 src 在 sensor 局部系下不在 AABB 内 |
| 真 U 槽永远 True | 反向问题：sensor AABB 套住 slider（如 sensor 是 slider 子物体，bounding box 在 slider 几何内部）|
| reload 后 `obj["is_triggered"]` 还残留 True | `is_invalid` 没命中 rebuild_geometry 末尾的 force_state；如果 sensor 是 valid 但 stale，需要仿真跑过一次 update 同步 |
| `sensor_invalid_geometry=True` 但你以为是真 U 槽 | `aspect_ratio ≤ 1.5` 命中。真 U 槽的 BB 长宽比应该 ≥ 1.5 |

## 6. 调试入口

```python
# 触发一次完整 tick 并打印所有 sensor 状态
import sys, importlib.util
for _n in list(sys.modules):
    if _n.startswith("modules") or "components" in _n:
        del sys.modules[_n]
import bpy
sys.path.insert(0, r"...\MotionSimulation")
_spec = importlib.util.spec_from_file_location(
    "motion_simulation_addon",
    r"...\MotionSimulation\addon.py",
)
addon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(addon)
mgr = addon.get_manager()
if mgr: mgr.update(1.0 / 60.0)
for obj in sorted(
    [o for o in bpy.data.objects if o.get("sensor_type") == "u_type"],
    key=lambda o: o.name,
):
    print(f"  {obj.name:24}  "
          f"triggered={bool(obj.get('is_triggered'))}  "
          f"invalid={bool(obj.get('sensor_invalid_geometry'))}")

# 离线查看 analyzer 内部状态
from modules.components.sensor.u_sensor import UTypeSensor
for obj in bpy.data.objects:
    if obj.get("sensor_type") == "u_type":
        g = UTypeSensor(obj, kind="trigger").geometry
        print(obj.name, g.opening_axis, g.slot_width_a, g.slot_width_b, g.is_invalid)
```

## 7. 局限性（应当注意）

1. **Pure-vertex 估算**：ASCII U 槽的"两臂间空隙"在 face 拓扑上，不在
   vertex 拓扑上。当前 `_calculate_slot_widths` 在采样到 cube-like 边沿
   时退化到 `bb_extent × 0.5`。精度损失但功能正常。
2. **fallback 路径不可信**：没有 vertex 时仅靠 `obj.bound_box` 推断。
   `_validate_extents` 提供部分兜底；如有需要可强制 `is_invalid=True`。
3. **stale state 需要仿真同步**：reload 后第一次仿真 tick 之前，
   `obj["is_triggered"]` 保留上一次写入的值。invalid sensor 的
   `rebuild_geometry` 末尾会主动清，valid sensor 需要正常 update 一帧
   同步。