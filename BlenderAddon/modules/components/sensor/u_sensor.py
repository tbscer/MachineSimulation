# -*- coding: utf-8 -*-
"""U 型槽式传感器（U Type Sensor）。

检测方式（简单直接）：

- 用户在 Blender 里手动设置 sensor 的“感应方向” ``sensor_direction``
  （X / Y / Z）与厚度 ``sensor_thickness``（默认 1 mm = 0.001 m）。
- 在 sensor 局部坐标系下，沿感应方向以 sensor 局部 BB 中心为中心、
  厚度为 ``sensor_thickness`` 切一个薄片，其余两维用 sensor 局部 BB
  完整尺寸。形成一个 thin "detection box"。
- 把遮挡物（slider/shim）的 8 个包围盒角点变换到 sensor 局部坐标系，
  保留其朝向（OBB），与触发薄片做 SAT 精确相交判定；重叠即触发。
  对轴向对齐（无旋转）的物体，结果与简单 AABB 重叠完全一致。

不再使用任何 vertex 启发（occupancy / 密度不对称 / 形状比例）。几何
仅仅保留 sensor mesh 的局部 AABB，方向/尺寸由用户在 UI 上配置，避免
了 vertex-only 算法无法区分 cube 占位与真 ASCII U 槽的问题。
"""

from __future__ import annotations

from dataclasses import dataclass

from .base_sensor import BaseSensor
from .geometry import (
    DetectionVolume,
    EPSILON,
    Vector,
    get_object_world_corners,
)


# EnumProperty 的字符串选项到 axis 索引的映射
_DIRECTION_INDEX = {"X": 0, "Y": 1, "Z": 2}


@dataclass
class UTypeGeometry:
    """Sensor 的静态几何信息。"""

    # Sensor mesh 在 sensor 局部坐标系下的 AABB
    sensor_min_local: Vector
    sensor_max_local: Vector

    # DetectionVolume（保留以兼容旧代码可能引用的接口）
    detection_volume: DetectionVolume


class UTypeGeometryAnalyzer:
    """几何分析器 —— 当前只负责取 sensor 的局部 AABB。"""

    def analyze(self, obj, depsgraph=None) -> UTypeGeometry:
        bb_min, bb_max = self._bound_box(obj, depsgraph)
        center = (bb_min + bb_max) * 0.5
        detection_volume = DetectionVolume(
            center=self._world_point(obj, center),
            normal=Vector((0.0, 0.0, 1.0)),
            width=bb_max[0] - bb_min[0],
            height=bb_max[1] - bb_min[1],
            local_center=center,
            local_x=Vector((1.0, 0.0, 0.0)),
            local_z=Vector((0.0, 0.0, 1.0)),
        )
        return UTypeGeometry(
            sensor_min_local=bb_min,
            sensor_max_local=bb_max,
            detection_volume=detection_volume,
        )

    def _bound_box(self, obj, depsgraph=None):
        """拿到 sensor mesh 在 sensor 局部坐标系下的 AABB。"""
        eval_obj = (
            obj.evaluated_get(depsgraph) if depsgraph is not None else obj
        )
        bb = getattr(eval_obj, "bound_box", None)
        if bb is None or len(bb) < 8:
            zero = Vector((0.0, 0.0, 0.0))
            return zero, zero
        mn = Vector((
            min(c[0] for c in bb),
            min(c[1] for c in bb),
            min(c[2] for c in bb),
        ))
        mx = Vector((
            max(c[0] for c in bb),
            max(c[1] for c in bb),
            max(c[2] for c in bb),
        ))
        return mn, mx

    @staticmethod
    def _world_point(obj, local: Vector) -> Vector:
        """局部坐标转世界坐标；对 mock 残缺 matrix_world 保持宽容。"""
        mw = getattr(obj, "matrix_world", None)
        if mw is None:
            return local.copy()
        try:
            return mw @ local
        except Exception:
            return local.copy()


class UTypeSensor(BaseSensor):
    """U 型 Sensor。运行时按 direction + thickness 做 AABB overlap 检测。"""

    __slots__ = (
        "geometry",
        "_geometry_dirty",
    )

    # 默认厚度（米）。单位按用户当前场景（米）配置；用户后续会把绘图尺寸
    # 整体改成 mm，届时这里的默认值也随场景单位改即可。
    # 默认 1 mm：shim 通常较长，长 shim 跨过薄平面时逐帧 AABB 必然有
    # 一帧重叠，不会因步长大于厚度而漏检。
    DEFAULT_THICKNESS = 0.001

    def __init__(
        self,
        obj,
        kind: str = "trigger",
        depsgraph=None,
    ):
        super().__init__(obj, kind=kind)
        self.geometry = None
        self._geometry_dirty = False
        self.rebuild_geometry(depsgraph=depsgraph)

    # ---------------------------------------------------------
    # 几何
    # ---------------------------------------------------------

    def rebuild_geometry(self, depsgraph=None):
        analyzer = UTypeGeometryAnalyzer()
        self.geometry = analyzer.analyze(self.obj, depsgraph=depsgraph)
        self._geometry_dirty = False
        self._write_debug_properties()

    # ---------------------------------------------------------
    # 用户配置
    # ---------------------------------------------------------

    @property
    def direction(self) -> int:
        """读取 ``sensor_direction``（X / Y / Z），回 0/1/2。"""
        try:
            value = self.obj.sensor_direction  # EnumProperty 返回 "X"/"Y"/"Z"
            return _DIRECTION_INDEX.get(value, 0)
        except Exception:
            pass
        # 兼容历史：用 int 0/1/2 直接存储
        try:
            legacy = self.obj.get("sensor_direction", 0)
            if isinstance(legacy, (int, float)):
                return int(legacy) % 3
            if isinstance(legacy, str):
                return _DIRECTION_INDEX.get(legacy, 0)
        except Exception:
            pass
        return 0

    @property
    def thickness(self) -> float:
        # 旧版把厚度存在 custom property ``sensor_thickness``；新版在
        # ui.py 里注册了 RNA 属性 ``Object.sensor_thickness``（host 面板
        # 可直接编辑）。两者都认：custom key 优先，其次 RNA 属性。
        try:
            value = self.obj.get("sensor_thickness", None)
            if value is not None:
                return max(float(value), EPSILON)
        except Exception:
            pass
        try:
            value = getattr(self.obj, "sensor_thickness", None)
            if value is not None:
                return max(float(value), EPSILON)
        except Exception:
            pass
        return self.DEFAULT_THICKNESS

    # ---------------------------------------------------------
    # 检测盒计算
    # ---------------------------------------------------------

    def _detection_box_local(self):
        """构造 sensor 局部坐标系下的 detection box（``(list, list)``，
        分别是 AABB min / max）。

        沿 ``direction`` 方向以 sensor 局部 BB 中心为中心、厚度为
        ``thickness`` 切一个薄片；其余两维用 sensor 局部 BB 完整尺寸。

        返回 list 而非 Vector：geometry 模块的 fallback Vector 是
        ``tuple`` 子类，immutable，不能 ``box_min[d] = ...``。list
        同时保持索引访问顺序，调用方使用 ``box_min[i]`` ``box_max[i]``
        即可。
        """
        bb_min = self.geometry.sensor_min_local
        bb_max = self.geometry.sensor_max_local
        d = self.direction
        if d < 0 or d > 2:
            d = 0
        c = (bb_min[d] + bb_max[d]) * 0.5
        box_min = [bb_min[0], bb_min[1], bb_min[2]]
        box_max = [bb_max[0], bb_max[1], bb_max[2]]
        box_min[d] = c - self.thickness * 0.5
        box_max[d] = c + self.thickness * 0.5
        return box_min, box_max

    # ---------------------------------------------------------
    # 检测
    # ---------------------------------------------------------

    # 局部叉乘/长度，避免依赖 mathutils 的具体 API（离线 fallback Vector
    # 没有 cross/length），同时 Blender 环境两者都可运行。
    @staticmethod
    def _cross3(a, b):
        return Vector((
            a.y * b.z - a.z * b.y,
            a.z * b.x - a.x * b.z,
            a.x * b.y - a.y * b.x,
        ))

    @staticmethod
    def _dot3(a, b):
        return a.x * b.x + a.y * b.y + a.z * b.z

    @staticmethod
    def _len3(a):
        return (a.x * a.x + a.y * a.y + a.z * a.z) ** 0.5

    def _get_source_local_corners(self, source_obj, depsgraph=None):
        """把 source 的 8 个包围盒角点变换到 sensor 局部坐标系。

        不坍缩成 AABB：保留 source 相对 sensor 的朝向（OBB）。旋转的
        运动体（如 RotateAxis 的 shim）在 sensor 系里是有向的盒子，
        若在这里取 min/max 会撑出一个远比实体大的轴对齐盒——在检测
        薄片时就会“提前触发”（shim 还没碰到触发平面就停下）。
        """
        world_corners = get_object_world_corners(
            source_obj,
            depsgraph=depsgraph,
        )
        try:
            sensor_inverse = self.obj.matrix_world.inverted()
        except Exception:
            sensor_inverse = None
        if sensor_inverse is None:
            return world_corners  # 拿不到逆矩阵：退化处理
        return [sensor_inverse @ p for p in world_corners]

    def _get_source_local_aabb(self, source_obj, depsgraph=None):
        """把 source 的世界包围盒 8 个角点变换到 sensor 局部系，重新
        取 AABB。保留给兼容/调试使用；检测本身走
        :meth:`_oriented_box_overlaps_aabb`（精确 OBB 相交）。"""
        local_corners = self._get_source_local_corners(
            source_obj, depsgraph=depsgraph,
        )
        min_v = Vector((
            min(v.x for v in local_corners),
            min(v.y for v in local_corners),
            min(v.z for v in local_corners),
        ))
        max_v = Vector((
            max(v.x for v in local_corners),
            max(v.y for v in local_corners),
            max(v.z for v in local_corners),
        ))
        from .geometry import AABB
        return AABB(min=min_v, max=max_v)

    def _oriented_box_overlaps_aabb(self, corners, box_min, box_max) -> bool:
        """SAT 判定：sensor 局部系里的 AABB 薄片（box_min..box_max）与
        source 的 OBB（8 个角点 ``corners``）是否真实相交。

        只对 AABB 相交（简单重叠）在旋转体上是过度保守的：例如一个绕
        Z 轴旋转的 shim，其 AABB 会覆盖比实体大得多的范围，shim 还没
        碰到触发面就已经“重叠”。这里用分离轴定理，只汇报真实几何接触，
        与直线轴（无旋转，两者等价）的触发时机一致。
        """
        # source OBB 的三个轴（来自角点构成的边缘向量）
        e1 = corners[1] - corners[0]
        e2 = corners[3] - corners[0]
        e3 = corners[4] - corners[0]
        box_axes = []
        for e in (e1, e2, e3):
            n = self._len3(e)
            if n > EPSILON:
                box_axes.append(Vector((e.x / n, e.y / n, e.z / n)))
        # 15 个候选分离轴：source 3 轴 + 薄片 3 轴 + 9 个叉积。
        # 薄片是 axis-aligned，三条标准轴就是它的面法向。
        axes = list(box_axes) + [
            Vector((1.0, 0.0, 0.0)),
            Vector((0.0, 1.0, 0.0)),
            Vector((0.0, 0.0, 1.0)),
        ]
        for a in box_axes:
            for b in axes[3:6]:
                ax = self._cross3(a, b)
                if self._len3(ax) > EPSILON:
                    axes.append(ax)
        # 薄片的 8 个角点
        slab_corners = [
            Vector((box_min[0], box_min[1], box_min[2])),
            Vector((box_max[0], box_min[1], box_min[2])),
            Vector((box_min[0], box_max[1], box_min[2])),
            Vector((box_max[0], box_max[1], box_min[2])),
            Vector((box_min[0], box_min[1], box_max[2])),
            Vector((box_max[0], box_min[1], box_max[2])),
            Vector((box_min[0], box_max[1], box_max[2])),
            Vector((box_max[0], box_max[1], box_max[2])),
        ]
        tol = 1e-9
        for ax in axes:
            lo_a = min(self._dot3(p, ax) for p in corners)
            hi_a = max(self._dot3(p, ax) for p in corners)
            lo_b = min(self._dot3(p, ax) for p in slab_corners)
            hi_b = max(self._dot3(p, ax) for p in slab_corners)
            if hi_a < lo_b - tol or lo_a > hi_b + tol:
                return False  # 找到一条分离轴 → 不相交
        return True

    def _detect(self, source_obj, depsgraph=None) -> bool:
        """True 当且仅当 source 实体（OBB）与 sensor 的触发薄片真实相交
        （SAT 判定，sensor 局部坐标系）。不旋转/轴向对齐时结果与旧的
        AABB 重叠一致。"""
        if self.geometry is None:
            self.rebuild_geometry(depsgraph=depsgraph)
        geometry = self.geometry

        # 退化保护：sensor BB 全维度都坍缩为零（无几何 / 占位 mesh），
        # 不可能触发。
        extents = [
            geometry.sensor_max_local[i] - geometry.sensor_min_local[i]
            for i in range(3)
        ]
        if max(extents) <= EPSILON:
            return False

        local_corners = self._get_source_local_corners(
            source_obj,
            depsgraph=depsgraph,
        )
        box_min, box_max = self._detection_box_local()
        return self._oriented_box_overlaps_aabb(
            local_corners, box_min, box_max,
        )

    # ---------------------------------------------------------
    # Debug 属性
    # ---------------------------------------------------------

    def _write_debug_properties(self):
        try:
            obj = self.obj
            obj["sensor_type"] = "u_type"
            box_min, box_max = self._detection_box_local()
            obj["detection_min_x"] = float(box_min[0])
            obj["detection_min_y"] = float(box_min[1])
            obj["detection_min_z"] = float(box_min[2])
            obj["detection_max_x"] = float(box_max[0])
            obj["detection_max_y"] = float(box_max[1])
            obj["detection_max_z"] = float(box_max[2])
            obj["sensor_direction_axis"] = self.direction
            obj["sensor_thickness_value"] = self.thickness
        except Exception:
            pass

    # ---------------------------------------------------------
    # 公共 API（保持兼容）
    # ---------------------------------------------------------

    def reset_runtime(self):
        """无逐帧状态 —— 空操作。"""
        return None

    def get_detection_volume(self):
        if self.geometry is None:
            return None
        return self.geometry.detection_volume