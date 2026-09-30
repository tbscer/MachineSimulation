"""ApproachSensor host 命名判定。

与 :mod:`modules.Sensor.naming` 同构(从那里整体重命名而来)。

约定
----
- host 名字必须以 ``ApproachSensor`` 开头,后接非空后缀(如
  ``ApproachSensor1`` / ``ApproachSensor_Main`` / ``ApproachSensor.001``)。
- host 同时是**配置容器** (``approach_sensor`` PropertyGroup, 只有一个
  ``enabled`` 开关)、**感应区锚点** (虚拟立方体的坐标系 = host 的
  局部坐标系)。
- :class:`ApproachSensorModule` 不产运动,只做 IO 感知,所以 ``is_host``
  不限制 type: MESH / EMPTY 都行 —— 艺术家通常把代表 sensor 的
  **立方体 MESH** 直接命名 ``ApproachSensor1``, 它自己就是感应区锚点。

注意
----
- LinearAxis / RotateAxis / Cylinder 都用 EMPTY host (其设计强制 EMPTY,
  见各自 ``naming.py``); ApproachSensor 允许更灵活 —— MESH 是默认形态
  (因为 ApproachSensor 几何以 mesh 局部系为参考),但 host 是 EMPTY 时
  discovery 会在 build 时 print 提示并跳过, 因为 empty 没有 ``bound_box``
  无法做 SAT。

normal_axis 字段已废弃
-----------------------
旧版 ApproachSensor 有一个 ``normal_axis`` (X/Y/Z) 字段, 描述"cube 从
``working_face_center`` 沿 +normal_axis 方向延伸"。该设计与"中心对齐 +
长宽高"的 box 模型重复, 容易让 artist 误配导致 cube 陷进物体内部。

新版去除 ``normal_axis``, 用中心对齐的轴对齐盒直接描述感应区:
- ``cube_size`` (长宽高) + ``working_face_center`` (中心偏移)
- artist 在 UI 上设长宽高 + 偏离中心位置即可, 不需考虑方向。

旧版 ``Sensor`` 名称与 LinearAxis 的 ``sensor_direction`` 等字段名冲突,
已统一改成 ``ApproachSensor`` 前缀以避免歧义。
"""

from __future__ import annotations


HOST_PREFIX = "ApproachSensor"


def is_host(obj) -> bool:
    """Return True iff ``obj`` is an ApproachSensor host.

    判定:
    1. obj 不为 None (且名字读取不抛 ``ReferenceError``);
    2. obj.name 以 :data:`HOST_PREFIX` 开头;
    3. 去掉前缀后剩余须非空 (挡掉 ``"ApproachSensor"`` 这种没 id 的名字)。

    **不限制 type** —— MESH / EMPTY 都接受。实际 build 时再按 type
    区分: MESH + ``sensor_type == "approach"`` 才能成功 build;
    EMPTY 在 build 阶段 print 提示并跳过 (感应区几何以 mesh 局部系
    为参考, 没有 mesh 就没法做 SAT)。
    """
    if obj is None:
        return False
    try:
        name = getattr(obj, "name", "") or ""
    except ReferenceError:
        # 已删除的 datablock (stale 引用) —— 当作不是 host, 而不是抛异常。
        return False
    except Exception:
        return False
    if not isinstance(name, str) or not name.startswith(HOST_PREFIX):
        return False
    suffix = name[len(HOST_PREFIX):]
    stripped = suffix.lstrip("_")
    return bool(stripped)


__all__ = ["HOST_PREFIX", "is_host"]