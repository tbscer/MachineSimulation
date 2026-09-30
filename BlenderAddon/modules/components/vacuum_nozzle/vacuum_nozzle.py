# -*- coding: utf-8 -*-
"""VacuumNozzle 几何传感器:沿法向的虚拟立方体 + 单对象 in-area 查询。

锚点
----
锚点是**真空吸嘴自己**(``VacuumNozzle*`` host),不再需要单独的
``sensor_mesh`` —— 吸嘴的局部坐标系就是感应区的坐标系。

设计要点
--------
- 几何模型与 :class:`ApproachSensor` 完全一致:锚点对象局部坐标系
  下,以 ``working_face_center`` 为起点,沿 ``normal_axis``(X/Y/Z)
  伸出 ``cube_size[normal_axis]`` 长度的虚拟立方体,横截面尺寸
  ``cube_size[other_axes]``。
- 几何数学(SAT 重叠判定 + world↔local 变换 + cube 边界计算)
  直接复用 :mod:`modules.components.approach_sensor.approach_sensor`
  的内部 helper。**有意保留 leading underscore 的私有符号** —— 它们
  在 approach_sensor 模块之外没有"公共 API 承诺",但语义稳定;
  真空吸嘴不走 ApproachSensor 的 ``update()`` 翻转状态机(那只是
  ApproachSensor 对外契约的一部分,不是几何语义),所以这种耦合是
  显式的、可接受的。未来如果要重构,把这些 helper 提到
  ``modules/components/_cube_geometry.py`` 是直接的提取 —— 两个
  detector 看到的输入输出完全一致。
- 对外只暴露无状态的 ``is_in_area(target_obj)`` 查询 —— 与
  ApproachSensor 的 ``update()`` 翻转状态机不同,VacuumNozzleSensor
  不维护 ``is_triggered``。状态由 :class:`VacuumNozzleModule`
  的状态机持有。

写回约定
--------
本类**不写**任何自定义属性(不盖 ``sensor_type``)。overlay 靠
:func:`modules.VacuumNozzle.naming.is_host` 的名字前缀识别 host;
运行态的 ``vacuum_nozzle_on`` / ``vacuum_nozzle_holding`` /
``vacuum_nozzle_sensing`` 三个 bool 由
:class:`~modules.VacuumNozzle.runtime.VacuumNozzleModule` 每 tick 写回,
overlay 只读。
"""

from __future__ import annotations

try:
    from mathutils import Vector  # type: ignore
except ImportError:  # 离线 fallback,复用 sensor/geometry.py 的最小 Vector 替身
    from modules.components.sensor.geometry import Vector  # type: ignore


# 直接 import approach_sensor 的几何 helper(见 docstring 中的耦合说明)
#
# Blender addon 加载时,``MotionSimulation`` 作为顶级 package 被
# import,但 ``modules`` 不在 sys.path 顶层 —— 因此用绝对路径
# ``from modules.components.approach_sensor...`` 会 ``ModuleNotFoundError``。
# 必须走相对路径:从 ``vacuum_nozzle.py`` 上跳 4 层到 ``MotionSimulation``
# 根,然后 descend 进 ``modules.components.approach_sensor.approach_sensor``。
# 离线测试时 ``sys.path.insert(0, _PKG)`` 把 addon 根加入,
# ``modules.*`` 路径才走通 —— 所以仍保留 ``except`` 分支兜底。
# 注意: VacuumNozzle 仍保留 normal_axis 语义 (与 ApproachSensor 不同)
# —— 不复用 approach_sensor 的 _compute_cube_bounds (后者已去除 normal_axis),
# 在本文件内重新定义独立函数 _compute_cube_bounds 保留 normal_axis。
try:
    from ....modules.components.approach_sensor.approach_sensor import (  # type: ignore
        _oriented_box_overlaps_aabb,
        _trigger_world_corners,
        _world_to_local,
    )
except ImportError:  # 离线 fallback(直接脚本路径)
    from modules.components.approach_sensor.approach_sensor import (  # type: ignore  # noqa: F401
        _oriented_box_overlaps_aabb,
        _trigger_world_corners,
        _world_to_local,
    )


def _compute_cube_bounds(normal_axis, cube_size, working_face_center):
    """VacuumNozzle 专用: normal_axis 语义下算 sensor 局部系里的立方体 AABB 边界。

    与 approach_sensor 的同名函数不同: 这里保留 normal_axis, 让虚拟立方体从
    ``working_face_center`` 沿 +normal_axis 方向延伸 ``cube_size[normal_axis]``
    的长度 (探针型几何)。横截面以 working_face_center 为中心, 边长为
    ``cube_size[i]`` 与 ``cube_size[j]`` (另两轴)。
    """
    d = normal_axis
    cs = cube_size
    wc = working_face_center
    other = [i for i in range(3) if i != d]
    cube_min = [wc[0], wc[1], wc[2]]
    cube_max = [wc[0], wc[1], wc[2]]
    cube_min[d] = wc[d]
    cube_max[d] = wc[d] + cs[d]
    cube_min[other[0]] = wc[other[0]] - cs[other[0]] * 0.5
    cube_max[other[0]] = wc[other[0]] + cs[other[0]] * 0.5
    cube_min[other[1]] = wc[other[1]] - cs[other[1]] * 0.5
    cube_max[other[1]] = wc[other[1]] + cs[other[1]] * 0.5
    return cube_min, cube_max


# normal_axis 字符串到索引的映射
_NORMAL_AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}


# 默认值(与 RNA 注册处保持一致)
_DEFAULT_NORMAL_AXIS = 2           # Z
_DEFAULT_CUBE_SIZE = (1.0, 1.0, 3.0)
_DEFAULT_WORKING_FACE_CENTER = (0.0, 0.0, 0.0)


def _read_normal_axis(obj) -> int:
    """读 ``vacuum_nozzle_normal_axis``(EnumProperty 字符串或 int 0/1/2)。"""
    try:
        v = obj.vacuum_nozzle_normal_axis
        if isinstance(v, str):
            return _NORMAL_AXIS_INDEX.get(v, _DEFAULT_NORMAL_AXIS)
        if isinstance(v, (int, float)):
            return int(v) % 3
    except Exception:
        pass
    # 兜底:custom property
    try:
        v = obj.get("vacuum_nozzle_normal_axis", _DEFAULT_NORMAL_AXIS)
        if isinstance(v, str):
            return _NORMAL_AXIS_INDEX.get(v, _DEFAULT_NORMAL_AXIS)
        if isinstance(v, (int, float)):
            return int(v) % 3
    except Exception:
        pass
    return _DEFAULT_NORMAL_AXIS


def _read_cube_size(obj) -> tuple:
    """读 ``vacuum_nozzle_cube_size``(FloatVector(3))。默认 (1, 1, 3)。"""
    try:
        cs = obj.vacuum_nozzle_cube_size
        return (float(cs[0]), float(cs[1]), float(cs[2]))
    except Exception:
        pass
    try:
        cs = obj.get("vacuum_nozzle_cube_size", _DEFAULT_CUBE_SIZE)
        if cs is None or len(cs) < 3:
            return _DEFAULT_CUBE_SIZE
        return (float(cs[0]), float(cs[1]), float(cs[2]))
    except Exception:
        return _DEFAULT_CUBE_SIZE


def _read_working_face_center(obj) -> tuple:
    """读 ``vacuum_nozzle_working_face_center``,默认 (0, 0, 0)。"""
    try:
        c = obj.vacuum_nozzle_working_face_center
        return (float(c[0]), float(c[1]), float(c[2]))
    except Exception:
        pass
    try:
        c = obj.get(
            "vacuum_nozzle_working_face_center",
            _DEFAULT_WORKING_FACE_CENTER,
        )
        if c is None or len(c) < 3:
            return _DEFAULT_WORKING_FACE_CENTER
        return (float(c[0]), float(c[1]), float(c[2]))
    except Exception:
        return _DEFAULT_WORKING_FACE_CENTER


# ---- 主类 -----------------------------------------------------------


class VacuumNozzleSensor:
    """虚拟立方体拣选检测器(无状态查询接口)。

    与 :class:`ApproachSensor` 的区别:
    - 不维护 ``_is_triggered`` 翻转状态机(由 VacuumNozzleModule 持有)。
    - 不写 ``obj["is_triggered"]``(避免污染 ApproachSensor 的语义)。
    - 对外只暴露 ``is_in_area(target_obj)`` 单查询。
    """

    __slots__ = (
        "obj",                   # host 对象(感应区锚点 / 可视化载体)
        "_normal_axis",          # 0/1/2 (host 局部 X/Y/Z)
        "_cube_size",            # (x, y, z)
        "_working_face_center",  # (x, y, z)
    )

    def __init__(self, sensor_obj):
        self.obj = sensor_obj
        self._normal_axis = _read_normal_axis(sensor_obj)
        self._cube_size = _read_cube_size(sensor_obj)
        self._working_face_center = _read_working_face_center(sensor_obj)

    def refresh(self) -> None:
        """重新从 RNA 读一遍几何配置。

        感应区尺寸 / 朝向可以在面板里随时改,而 runtime 的 sensor 实例是
        构造时建好并复用的 —— 不刷新就会一直用旧几何。三个属性读取很便宜,
        runtime 每 tick 调一次。
        """
        self._normal_axis = _read_normal_axis(self.obj)
        self._cube_size = _read_cube_size(self.obj)
        self._working_face_center = _read_working_face_center(self.obj)

    # ---- public properties ----

    @property
    def normal_axis(self) -> int:
        return self._normal_axis

    @property
    def cube_size(self) -> tuple:
        return self._cube_size

    @property
    def working_face_center(self) -> tuple:
        return self._working_face_center

    # ---- 单查询 ----

    def is_in_area(self, target_obj, depsgraph=None) -> bool:
        """``target_obj`` 的世界 AABB 是否进入虚拟立方体?

        无状态查询 —— 每次都重新走几何检测,不维护触发位。
        ``target_obj=None`` 或拿不到 world AABB 时返回 False。
        """
        if target_obj is None:
            return False
        world_corners = _trigger_world_corners(target_obj, depsgraph)
        if world_corners is None:
            return False
        # 把 8 个角点变换到 sensor 局部系
        local_corners = []
        for w in world_corners:
            lp = _world_to_local(self.obj, w)
            if lp is None:
                return False
            local_corners.append(lp)
        # 算 cube 边界
        cube_min, cube_max = _compute_cube_bounds(
            self._normal_axis, self._cube_size, self._working_face_center
        )
        # 退化保护:立方体全维度坍缩为零 → 不可能触发
        extent_d = cube_max[self._normal_axis] - cube_min[self._normal_axis]
        other = [i for i in range(3) if i != self._normal_axis]
        if (
            extent_d <= 1e-9
            or (cube_max[other[0]] - cube_min[other[0]]) <= 1e-9
            or (cube_max[other[1]] - cube_min[other[1]]) <= 1e-9
        ):
            return False
        return _oriented_box_overlaps_aabb(
            local_corners, cube_min, cube_max
        )

    # ---- 给 overlay 用的几何 ----

    def get_cube_world_corners(self):
        """返回虚拟立方体的 8 个世界角点(list of mathutils.Vector),失败返回 None。"""
        try:
            mw = self.obj.matrix_world
            if mw is None:
                return None
        except Exception:
            return None
        cube_min, cube_max = _compute_cube_bounds(
            self._normal_axis, self._cube_size, self._working_face_center
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

    def get_normal_axis_world_endpoints(self, length=None):
        """返回 normal axis 在 sensor 局部系里 [working_face_center,
        working_face_center + normal * length] 的世界端点(2 个 Vector),
        给 overlay 画法向白线用。失败返回 None。
        """
        try:
            mw = self.obj.matrix_world
            if mw is None:
                return None
        except Exception:
            return None
        wc = self._working_face_center
        d = self._normal_axis
        if length is None:
            length = self._cube_size[d] + 5.0  # 默认长度 +5 mm 突出
        unit = [0.0, 0.0, 0.0]
        unit[d] = 1.0
        local_start = Vector((wc[0], wc[1], wc[2]))
        local_end = Vector((
            wc[0] + unit[0] * length,
            wc[1] + unit[1] * length,
            wc[2] + unit[2] * length,
        ))
        try:
            return mw @ local_start, mw @ local_end
        except Exception:
            return None

    def __repr__(self) -> str:
        return (
            f"VacuumNozzleSensor(host={self.obj.name!r}, "
            f"normal_axis={self._normal_axis}, cube_size={self._cube_size}, "
            f"working_face_center={self._working_face_center})"
        )


__all__ = ["VacuumNozzleSensor"]