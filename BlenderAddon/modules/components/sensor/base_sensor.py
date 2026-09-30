from __future__ import annotations

from abc import ABC, abstractmethod


SENSOR_KINDS = (
    "trigger",
    "home",
    "front_limit",
    "back_limit",
    "pos_limit",
    "neg_limit",
)

LIMIT_SENSOR_KINDS = (
    "front_limit",
    "back_limit",
    "pos_limit",
    "neg_limit",
)


def aggregate_axis_sensor_states(sensors) -> dict:
    """把 axis 持有的 sensor 列表聚合成 axis-agnostic 的三布尔字段。

    返回字典的键固定为 ``home``、``pos_limit``、``neg_limit``；
    LinearAxis 用 ``back_limit`` 表示 pos、``front_limit`` 表示 neg
    （见 :mod:`modules.LinearAxix.discovery` 的命名映射），
    RotateAxis 直接用 ``pos_limit`` / ``neg_limit``。

    多个同类型 sensor 的状态按 OR 聚合（任意一个触发即视为触发）。

    接收任何带有 ``kind`` 和 ``is_triggered`` 属性的对象，
    不强制要求是 :class:`BaseSensor` 子类——方便单元测试用 mock。
    """
    home = False
    pos = False
    neg = False
    for s in sensors:
        if not getattr(s, "is_triggered", False):
            continue
        k = getattr(s, "kind", None)
        if k == "home":
            home = True
        elif k in ("back_limit", "pos_limit"):
            pos = True
        elif k in ("front_limit", "neg_limit"):
            neg = True
    return {"home": home, "pos_limit": pos, "neg_limit": neg}


class BaseSensor(ABC):
    """
    所有 Sensor 的基础类。

    BaseSensor 不关心具体 Sensor 的物理结构。

    U 型 Sensor、接近 Sensor、光电 Sensor 等，
    都通过 _detect() 实现自己的检测逻辑。
    """

    __slots__ = (
        "obj",
        "name",
        "kind",
        "is_home",
        "_is_triggered",
        "_last_change_frame",
    )

    def __init__(
        self,
        obj,
        kind: str = "trigger",
    ):
        self.obj = obj
        self.name = obj.name

        if kind not in SENSOR_KINDS:
            kind = "trigger"

        self.kind = kind
        self.is_home = kind == "home"

        try:
            self._is_triggered = bool(
                obj.get("is_triggered", False)
            )
        except Exception:
            self._is_triggered = False

        try:
            obj["is_triggered"] = self._is_triggered
        except Exception:
            pass

        self._last_change_frame = -1

    @property
    def is_triggered(self) -> bool:
        return self._is_triggered

    def update(
        self,
        source_obj,
        depsgraph=None,
    ) -> bool:
        """
        更新 Sensor。

        返回：
            True  = 状态发生变化
            False = 状态没有变化
        """

        try:
            triggered = bool(
                self._detect(
                    source_obj,
                    depsgraph=depsgraph,
                )
            )
        except Exception:
            triggered = False

        if triggered == self._is_triggered:
            return False

        self._is_triggered = triggered

        try:
            self.obj["is_triggered"] = triggered
        except Exception:
            pass

        self._stamp_frame()

        return True

    @abstractmethod
    def _detect(
        self,
        source_obj,
        depsgraph=None,
    ) -> bool:
        """
        子类实现具体检测算法。
        """
        raise NotImplementedError

    def _stamp_frame(self):
        try:
            import bpy

            self._last_change_frame = int(
                bpy.context.scene.frame_current
            )

        except Exception:
            self._last_change_frame = -1

    def force_state(self, state: bool):
        """
        强制设置 Sensor 状态。
        """

        state = bool(state)

        if state == self._is_triggered:
            return False

        self._is_triggered = state

        try:
            self.obj["is_triggered"] = state
        except Exception:
            pass

        self._stamp_frame()

        return True