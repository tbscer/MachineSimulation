# -*- coding: utf-8 -*-
"""ApproachSensor:独立组件,用虚拟盒检测 trigger_obj 是否进入触发区。

设计要点
--------
- sensor 物体上有 ``working_face_center`` (sensor 局部坐标系下的一个
  3D 点) + ``cube_size`` (长宽高) —— 在 sensor 局部系下定义一个
  **中心对齐的轴对齐盒** (axis-aligned box)。
- 虚拟盒边界 (sensor 局部系):
  - ``cube_min[i] = working_face_center[i] - cube_size[i] / 2``
  - ``cube_max[i] = working_face_center[i] + cube_size[i] / 2``
  (i ∈ {0, 1, 2})
- 默认 ``cube_size = (1, 1, 1)``, ``working_face_center = (0, 0, 0)``:
  sensor 局部原点为中心, 各方向 ±0.5 的 1×1×1 立方体。
- 检测算法: 把 trigger_obj 的 8 个世界 AABB 角点变换到 sensor 局部
  坐标系, 得到 trigger 在 sensor 局部系里的 OBB; 与 sensor 局部系
  里的 AABB 立方体做 SAT 判定。立方体在 sensor 局部系里是 AABB,
  trigger 是 OBB, 所以跑 15 轴分离轴 (3 个 trigger OBB + 3 个立方体
  AABB + 9 个叉积)。

不需 normal_axis
----------------
旧版 ApproachSensor 有一个 ``normal_axis`` (X/Y/Z) 字段, 让立方体从
``working_face_center`` 沿 +normal_axis 方向延伸。该设计引入了额外的
方向概念, 但实际使用中 "立方体" 描述的是 sensor 周围的"感应区", 而不是
"沿某个方向凸出的探针"。新版去除 ``normal_axis`` 字段, 用更直观的
``working_face_center`` (中心偏移) + ``cube_size`` (长宽高) 描述, 中心
对齐, 三个维度独立设置, 与典型 3D 编辑器里的 bounding box 习惯一致。

artist 调整建议:

- ``working_face_center`` 是 box 中心在 sensor 局部系的位置; 默认 (0, 0, 0)
  表示 box 与 sensor mesh 同心。artist 想让感应区偏离 sensor mesh 时,
  直接调 offset 即可。
- ``cube_size`` 三个分量分别表示 x / y / z 三个方向的尺寸; 默认 (1, 1, 1)
  表示各方向 ±0.5 = 1 mm × 1 mm × 1 mm 的感应区, 但 Cylinder 习惯用
  (1, 1, 3) 让 z 方向更长的探针区。

为什么不继承 BaseSensor
----------------------
BaseSensor 把 sensor mesh 的局部 AABB 作为"触发盒"的几何源。ApproachSensor
的虚拟立方体与 mesh 大小无关, 可能比 mesh 大也可能比 mesh 小, 而且位置
是显式配置的 ``working_face_center``, 不能套用 BaseSensor 的
``obj.bound_box`` 自动取 AABB 的逻辑。所以单独实现, 不继承。

写回约定
--------
- ``obj["sensor_type"] = "approach"`` —— 给 overlay / 其它代码识别
- ``obj["is_triggered"]`` —— bool, 翻转时同步写回 (与 UTypeSensor 一致)
- ``obj["approach_last_change_frame"]`` —— int, 翻转时的 scene frame,
  调试用
"""

from __future__ import annotations

try:
    from mathutils import Vector  # type: ignore
except ImportError:  # 离线 fallback, 复用 sensor/geometry.py 的最小 Vector 替身
    from modules.components.sensor.geometry import Vector  # type: ignore


# 默认值 (与 RNA 注册处保持一致)
_DEFAULT_CUBE_SIZE = (1.0, 1.0, 1.0)
_DEFAULT_WORKING_FACE_CENTER = (0.0, 0.0, 0.0)


def _read_cube_size(obj) -> tuple:
    """读 ``approach_sensor_cube_size`` (FloatVector(3))。默认 (1, 1, 1)。"""
    try:
        cs = obj.approach_sensor_cube_size
        return (float(cs[0]), float(cs[1]), float(cs[2]))
    except Exception:
        pass
    try:
        cs = obj.get("approach_sensor_cube_size", _DEFAULT_CUBE_SIZE)
        if cs is None or len(cs) < 3:
            return _DEFAULT_CUBE_SIZE
        return (float(cs[0]), float(cs[1]), float(cs[2]))
    except Exception:
        return _DEFAULT_CUBE_SIZE


def _read_working_face_center(obj) -> tuple:
    """读 ``approach_sensor_working_face_center``, 默认 (0, 0, 0)。"""
    try:
        c = obj.approach_sensor_working_face_center
        return (float(c[0]), float(c[1]), float(c[2]))
    except Exception:
        pass
    try:
        c = obj.get(
            "approach_sensor_working_face_center",
            _DEFAULT_WORKING_FACE_CENTER,
        )
        if c is None or len(c) < 3:
            return _DEFAULT_WORKING_FACE_CENTER
        return (float(c[0]), float(c[1]), float(c[2]))
    except Exception:
        return _DEFAULT_WORKING_FACE_CENTER


# ---- 几何 helpers (局部 SAT, 与 UTypeSensor 风格一致) ------------------


def _cross3(a, b):
    return Vector((
        a.y * b.z - a.z * b.y,
        a.z * b.x - a.x * b.z,
        a.x * b.y - a.y * b.x,
    ))


def _dot3(a, b):
    return a.x * b.x + a.y * b.y + a.z * b.z


def _len3(a):
    return (a.x * a.x + a.y * a.y + a.z * a.z) ** 0.5


def _oriented_box_overlaps_aabb(corners, box_min, box_max, eps=1e-9):
    """SAT 判定: trigger OBB (8 角点) 与 sensor 局部 AABB 立方体相交?

    立方体在 sensor 局部坐标系下是 AABB, 三条标准轴就是它的面法向;
    trigger 是任意 OBB。两两组合得到 15 个候选分离轴, 任一分离则不相交。
    """
    EPS = 1e-9
    # source OBB 三轴 (从角点边缘向量导出)
    e1 = corners[1] - corners[0]
    e2 = corners[3] - corners[0]
    e3 = corners[4] - corners[0]
    box_axes = []
    for e in (e1, e2, e3):
        n = _len3(e)
        if n > EPS:
            box_axes.append(Vector((e.x / n, e.y / n, e.z / n)))
    # 立方体 AABB 三轴 (标准基)
    world_axes = [
        Vector((1.0, 0.0, 0.0)),
        Vector((0.0, 1.0, 0.0)),
        Vector((0.0, 0.0, 1.0)),
    ]
    axes = list(box_axes) + list(world_axes)
    for a in box_axes:
        for b in world_axes:
            ax = _cross3(a, b)
            if _len3(ax) > EPS:
                axes.append(ax)
    # AABB 立方体 8 角点
    slab_corners = [
        Vector((box_min[0], box_min[1], box_min[2])),
        Vector((box_max[0], box_min[1], box_min[2])),
        Vector((box_max[0], box_max[1], box_min[2])),
        Vector((box_min[0], box_max[1], box_min[2])),
        Vector((box_min[0], box_min[1], box_max[2])),
        Vector((box_max[0], box_min[1], box_max[2])),
        Vector((box_max[0], box_max[1], box_max[2])),
        Vector((box_min[0], box_max[1], box_max[2])),
    ]
    for ax in axes:
        lo_a = min(_dot3(p, ax) for p in corners)
        hi_a = max(_dot3(p, ax) for p in corners)
        lo_b = min(_dot3(p, ax) for p in slab_corners)
        hi_b = max(_dot3(p, ax) for p in slab_corners)
        if hi_a < lo_b - eps or lo_a > hi_b + eps:
            return False
    return True


def _world_to_local(obj, world_corner):
    """把世界角点变换到 sensor 局部。退化时 (无 matrix_world / 逆矩阵失败)
    返回 ``None``。
    """
    try:
        mw = obj.matrix_world
        if mw is None:
            return None
        inv = mw.inverted()
        return inv @ world_corner
    except Exception:
        return None


def _trigger_world_corners(trigger_obj, depsgraph=None):
    """拿 trigger 的 8 个世界 AABB 角点。退化返回 None。"""
    try:
        eval_obj = (
            trigger_obj.evaluated_get(depsgraph)
            if depsgraph is not None
            else trigger_obj
        )
        bb = eval_obj.bound_box
    except Exception:
        return None
    if bb is None or len(bb) < 8:
        return None
    try:
        mw = trigger_obj.matrix_world
        if mw is None:
            return None
        return [mw @ Vector(c) for c in bb]
    except Exception:
        return None


def _compute_cube_bounds(cube_size, working_face_center):
    """算 sensor 局部系里的立方体 AABB 边界 (中心对齐的盒子)。

    返回 ``([min_x, min_y, min_z], [max_x, max_y, max_z])`` (普通 list, 不是
    Vector), 调用方按索引访问。

    box 中心 = ``working_face_center`` (sensor 局部系下);
    box 半边长 = ``cube_size[i] / 2`` (i ∈ {0, 1, 2})。

    与 Cylinder / ApproachSensorModule 共用这套几何, 语义自动一致。
    """
    cs = cube_size
    wc = working_face_center
    cube_min = [wc[0] - cs[0] * 0.5, wc[1] - cs[1] * 0.5, wc[2] - cs[2] * 0.5]
    cube_max = [wc[0] + cs[0] * 0.5, wc[1] + cs[1] * 0.5, wc[2] + cs[2] * 0.5]
    return cube_min, cube_max


# ---- 主类 -----------------------------------------------------------


class ApproachSensor:
    """虚拟盒触发检测器 (中心对齐的轴对齐盒 + SAT 检测)。

    设计: cube_size / working_face_center 不缓存到 instance, 每次访问
    都重新读 RNA —— artist 在 UI 上调 cube_size / face_center 后下一 tick
    立即生效。Cylinder 这类“启动后长生命周期”的使用者不会被过时缓存干扰。
    """

    __slots__ = (
        "obj",                   # sensor mesh (锚点 / 可视化)
        "trigger_obj",           # 检测对象 (一般是 TouchShime)
        "_is_triggered",         # bool 状态缓存 (需要在 _detect 中翻转判断)
    )

    def __init__(self, sensor_obj, trigger_obj):
        self.obj = sensor_obj
        self.trigger_obj = trigger_obj
        # 初始状态: 从 obj["is_triggered"] 读, 默认 False
        try:
            self._is_triggered = bool(sensor_obj.get("is_triggered", False))
        except Exception:
            self._is_triggered = False
        # 写回 sensor_type (给 overlay / 其它代码识别); 不强制覆盖已有值
        try:
            if sensor_obj.get("sensor_type") != "approach":
                sensor_obj["sensor_type"] = "approach"
        except Exception:
            pass
        # 写回初始 is_triggered, 保证外部一致
        try:
            sensor_obj["is_triggered"] = self._is_triggered
        except Exception:
            pass

    # ---- public properties (每次访问重新读 RNA, 不缓存) ----

    @property
    def is_triggered(self) -> bool:
        return self._is_triggered

    @property
    def cube_size(self) -> tuple:
        """从 RNA 实时读 cube_size; artist 调整 UI 后下一 tick 生效。"""
        return _read_cube_size(self.obj)

    @property
    def working_face_center(self) -> tuple:
        """从 RNA 实时读 face_center; artist 调整 UI 后下一 tick 生效。"""
        return _read_working_face_center(self.obj)

    # ---- per-tick 更新 ----

    def update(self, depsgraph=None) -> bool:
        """跑一次检测; 状态翻转时返回 True, 否则 False。"""
        new_state = self._detect(depsgraph)
        if new_state == self._is_triggered:
            return False
        self._is_triggered = new_state
        # 写回 obj, 方便外部读取
        try:
            self.obj["is_triggered"] = new_state
            try:
                import bpy
                self.obj["approach_last_change_frame"] = int(
                    bpy.context.scene.frame_current
                )
            except Exception:
                self.obj["approach_last_change_frame"] = -1
        except Exception:
            pass
        return True

    # ---- 检测 ----

    def _detect(self, depsgraph=None) -> bool:
        if self.trigger_obj is None:
            return False
        world_corners = _trigger_world_corners(self.trigger_obj, depsgraph)
        if world_corners is None:
            return False
        # 把 8 个角点变换到 sensor 局部系
        local_corners = []
        for w in world_corners:
            lp = _world_to_local(self.obj, w)
            if lp is None:
                return False
            local_corners.append(lp)
        # 算 cube 边界 (实时从 RNA 读, 不缓存)
        cube_min, cube_max = _compute_cube_bounds(
            self.cube_size, self.working_face_center
        )
        # 退化保护: 盒任一维度坍缩为零 → 不可能触发
        if (
            (cube_max[0] - cube_min[0]) <= 1e-9
            or (cube_max[1] - cube_min[1]) <= 1e-9
            or (cube_max[2] - cube_min[2]) <= 1e-9
        ):
            return False
        return _oriented_box_overlaps_aabb(
            local_corners, cube_min, cube_max
        )

    # ---- 给 overlay 用的几何 ----

    def get_cube_world_corners(self):
        """返回虚拟盒的 8 个世界角点 (list of mathutils.Vector), 失败返回 None。"""
        try:
            mw = self.obj.matrix_world
            if mw is None:
                return None
        except Exception:
            return None
        # 实时从 RNA 读 cube_size / face_center (artist UI 调整后立即生效)
        cube_min, cube_max = _compute_cube_bounds(
            self.cube_size, self.working_face_center
        )
        local = [
            Vector((cube_min[0], cube_min[1], cube_min[2])),
            Vector((cube_max[0], cube_min[1], cube_min[2])),
            Vector((cube_max[0], cube_max[1], cube_min[2])),
            Vector((cube_min[0], cube_max[1], cube_min[2])),
            Vector((cube_min[0], cube_min[1], cube_max[2])),
            Vector((cube_max[0], cube_min[1], cube_max[2])),
            Vector((cube_max[0], cube_max[1], cube_max[2])),
            Vector((cube_min[0], cube_max[1], cube_max[2])),
        ]
        try:
            return [mw @ c for c in local]
        except Exception:
            return None

    def force_state(self, state: bool) -> bool:
        """强制覆盖状态 (主要用于测试 / debug); 状态翻转时返回 True。"""
        state = bool(state)
        if state == self._is_triggered:
            return False
        self._is_triggered = state
        try:
            self.obj["is_triggered"] = state
        except Exception:
            pass
        return True

    def __repr__(self) -> str:
        return (
            f"ApproachSensor(name={self.obj.name!r}, "
            f"cube_size={self.cube_size}, "
            f"working_face_center={self.working_face_center}, "
            f"is_triggered={self._is_triggered})"
        )


__all__ = ["ApproachSensor"]