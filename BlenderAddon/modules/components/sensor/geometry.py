# -*- coding: utf-8 -*-
"""Sensor 通用几何工具。

提供 AABB、检测平面（DetectionVolume）以及若干世界 / 局部
坐标变换辅助函数，供 U 型 Sensor 等具体传感器实现使用。

当运行环境没有 Blender 的 ``mathutils`` 模块时（离线单元测试
等场景），提供最小可用的 Vector / Matrix 替身，保证本包仍可导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

try:
    from mathutils import Vector, Matrix  # type: ignore
except ImportError:  # pragma: no cover - 离线 / 非 Blender 环境的退路
    # 最小替身实现，保证本包在没有 ``mathutils``（Blender 的向量
    # 模块）时仍可导入 —— 例如离线单元测试，或插件被不携带
    # Blender 标准库扩展的工具加载。替身只需覆盖本模块用到的
    # 最小接口面。
    class Vector(tuple):  # type: ignore[no-redef]
        def __new__(cls, xyz):
            x, y, z = (float(xyz[0]), float(xyz[1]), float(xyz[2]))
            instance = super().__new__(cls, (x, y, z))
            instance.x = x
            instance.y = y
            instance.z = z
            return instance

        def __add__(self, other):
            return Vector((self.x + other.x, self.y + other.y, self.z + other.z))

        def __sub__(self, other):
            return Vector((self.x - other.x, self.y - other.y, self.z - other.z))

        def __mul__(self, other):
            return Vector((self.x * other, self.y * other, self.z * other))

        __rmul__ = __mul__

        def dot(self, other):
            return self.x * other.x + self.y * other.y + self.z * other.z

        def normalized(self):
            n = (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5 or 1.0
            return Vector((self.x / n, self.y / n, self.z / n))

        def normalize(self):
            n = self.normalized()
            self.x, self.y, self.z = n.x, n.y, n.z
            return self

        def copy(self):
            return Vector((self.x, self.y, self.z))

        def __setitem__(self, idx, value):
            # mathutils.Vector 支持 ``v[idx] = value``；这里保持一致，
            # 让修改几何分量的代码路径（例如分析器沿开口轴调整
            # 中心点）在离线环境下也能工作。
            if idx == 0:
                self.x = float(value)
            elif idx == 1:
                self.y = float(value)
            elif idx == 2:
                self.z = float(value)
            else:
                raise IndexError(idx)

    class Matrix:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

        def inverted(self):
            return self

        def to_3x3(self):
            return self

        def __matmul__(self, other):
            return other


# 几何比较容差
EPSILON = 1e-6


@dataclass
class AABB:
    """轴对齐包围盒。"""

    min: Vector
    max: Vector

    @property
    def center(self) -> Vector:
        return (self.min + self.max) * 0.5

    @property
    def size(self) -> Vector:
        return self.max - self.min

    def contains_point(
        self,
        point: Vector,
        touching: bool = True,
        epsilon: float = EPSILON,
    ) -> bool:
        """判断点是否位于包围盒内部。"""
        if touching:
            return (
                self.min.x - epsilon <= point.x <= self.max.x + epsilon
                and self.min.y - epsilon <= point.y <= self.max.y + epsilon
                and self.min.z - epsilon <= point.z <= self.max.z + epsilon
            )

        return (
            self.min.x < point.x < self.max.x
            and self.min.y < point.y < self.max.y
            and self.min.z < point.z < self.max.z
        )

    def overlaps(
        self,
        other: "AABB",
        touching: bool = True,
        epsilon: float = EPSILON,
    ) -> bool:
        """判断两个包围盒是否相交。"""
        if touching:
            return (
                self.max.x >= other.min.x - epsilon
                and self.min.x <= other.max.x + epsilon
                and self.max.y >= other.min.y - epsilon
                and self.min.y <= other.max.y + epsilon
                and self.max.z >= other.min.z - epsilon
                and self.min.z <= other.max.z + epsilon
            )

        return (
            self.max.x > other.min.x
            and self.min.x < other.max.x
            and self.max.y > other.min.y
            and self.min.y < other.max.y
            and self.max.z > other.min.z
            and self.min.z < other.max.z
        )


@dataclass
class DetectionVolume:
    """
    一个无限薄的检测平面。

    center:
        检测平面的中心（世界坐标）。

    normal:
        检测平面的法向量，也就是 Sensor 的 opening direction。

    width:
        检测区域沿 ``local_x`` 方向的有效宽度。

    height:
        检测区域沿 ``local_z`` 方向的有效高度。

    local_center:
        Sensor 局部坐标系中的检测平面中心。

    local_x / local_z:
        检测平面的两个局部方向（单位向量）。注意它们不一定
        对应世界 / 局部的 X、Z 轴 —— 开口轴是哪个轴，这两个
        方向就由剩余两轴决定。
    """

    center: Vector
    normal: Vector

    width: float
    height: float

    # Sensor 局部坐标系中的中心
    local_center: Vector

    # 检测平面的两个局部方向
    local_x: Vector
    local_z: Vector

    def __post_init__(self):
        self.normal.normalize()

    def point_inside(
        self,
        point_world: Vector,
        sensor_matrix: Matrix,
        epsilon: float = EPSILON,
    ) -> bool:
        """
        判断一个世界坐标点是否位于检测平面的有效区域内。

        把点变换到 Sensor 局部坐标后，沿 ``local_x`` / ``local_z``
        两个平面方向做投影判断（而不是硬编码检查 x / z 分量），
        这样无论开口轴是哪个轴，结果都正确。
        """

        local = sensor_matrix.inverted() @ point_world

        half_width = self.width * 0.5
        half_height = self.height * 0.5

        offset = local - self.local_center

        return (
            abs(offset.dot(self.local_x)) <= half_width + epsilon
            and
            abs(offset.dot(self.local_z)) <= half_height + epsilon
        )


def get_world_aabb(obj, depsgraph=None) -> AABB:
    """
    获取 Blender Object 的世界 AABB。
    """

    eval_obj = (
        obj.evaluated_get(depsgraph)
        if depsgraph is not None
        else obj
    )

    matrix = obj.matrix_world

    corners = [
        matrix @ Vector(corner)
        for corner in eval_obj.bound_box
    ]

    min_v = Vector((
        min(v.x for v in corners),
        min(v.y for v in corners),
        min(v.z for v in corners),
    ))

    max_v = Vector((
        max(v.x for v in corners),
        max(v.y for v in corners),
        max(v.z for v in corners),
    ))

    return AABB(min=min_v, max=max_v)


def get_local_aabb_from_world_corners(
    world_corners: Sequence[Vector],
    inverse_matrix: Matrix,
) -> AABB:
    """
    将世界坐标点转换到 Sensor 局部坐标系后计算 AABB。
    """

    local = [
        inverse_matrix @ point
        for point in world_corners
    ]

    min_v = Vector((
        min(v.x for v in local),
        min(v.y for v in local),
        min(v.z for v in local),
    ))

    max_v = Vector((
        max(v.x for v in local),
        max(v.y for v in local),
        max(v.z for v in local),
    ))

    return AABB(min=min_v, max=max_v)


def get_object_world_corners(obj, depsgraph=None):
    """
    返回 Object 的 8 个世界坐标包围盒顶点。
    """

    eval_obj = (
        obj.evaluated_get(depsgraph)
        if depsgraph is not None
        else obj
    )

    matrix = obj.matrix_world

    return [
        matrix @ Vector(corner)
        for corner in eval_obj.bound_box
    ]


def project_range(
    points: Iterable[Vector],
    axis: Vector,
):
    """
    将一组点投影到 axis，返回 min/max。
    """

    axis = axis.normalized()

    values = [
        point.dot(axis)
        for point in points
    ]

    return min(values), max(values)


def signed_distance_to_plane(
    point: Vector,
    plane_point: Vector,
    plane_normal: Vector,
):
    """
    点到平面的有符号距离。
    """

    return (point - plane_point).dot(
        plane_normal.normalized()
    )