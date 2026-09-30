"""ApproachSensor module package —— 被动 Approach sensor module 的运行时实现。

与 :mod:`modules.Sensor` 同构,但 host 命名 / PropertyGroup / kind / category
全部统一为 ``ApproachSensor*`` 命名。**重命名原因**：

- LinearAxis 的 :class:`UTypeSensor` 已经在 ``bpy.types.Object`` 上注册了
  ``sensor_direction`` / ``sensor_thickness`` / ``home_sensor`` /
  ``pos_sensor`` / ``neg_sensor`` 等属性,共享名 ``sensor`` 容易混淆;
- 单独 ``Sensor*`` host 命名会让 dev_panel 与 RPC 客户端难以判别"哪个
  sensor 模块"。
- :mod:`modules.components.approach_sensor` 已经存在,``modules.Cylinder``
  也按 ``approach_sensor_*`` 命名引用,本 module 与之对齐后,scene 里
  一旦出现 ``sensor_type="approach"`` mesh,artist 一眼就能定位到这是
  ``ApproachSensor`` module。

公共 API:
- :class:`ApproachSensorModule` —— 运行时模块
- :func:`register` / :func:`unregister` —— PropertyGroup + UI 注册入口
"""

from __future__ import annotations

try:
    from .runtime import ApproachSensorModule
except ImportError:  # pragma: no cover
    from modules.ApproachSensor.runtime import ApproachSensorModule  # noqa: F401


def register() -> None:
    """注册 ApproachSensorProperty 到 ``bpy.types.Object.approach_sensor`` + UI panel。

    在 ``MotionSimulation.addon.register`` 里调用。
    """
    from . import component, ui
    component.register()
    ui.register()


def unregister() -> None:
    """反注册 (幂等)。"""
    from . import component, ui
    ui.unregister()
    component.unregister()


__all__ = ["ApproachSensorModule", "register", "unregister"]