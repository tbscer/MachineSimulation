from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SimulationCommand:
    """Lightweight command object for future module expansion."""
    action: str
    payload: Dict[str, Any] = field(default_factory=dict)


class BaseSimulationModule:
    """Base contract for all runtime modules used by SimulationManager."""

    def __init__(self, *, module_id: str, kind: str, category: Optional[str] = None):
        self.module_id = module_id
        self.kind = kind
        # ``category`` 是 :class:`SimulationManager.snapshot` 里 state_push
        # 数据按类分组的 key。当前 LinearAxis / RotateAxisRuntime 都传
        # ``category="axes"``，这样多个 kind 可以归到一个 category；
        # 后续添加其他类别时（例如 ``"cameras"`` / ``"collisions"``）
        # 可以直接用 ``category="cameras"``。默认 ``category == kind``，
        # 单 kind 单 category 的 module 可以不显式传。
        self.category = category if category is not None else kind
        # ``_alive`` is set to ``False`` by the runtime when it
        # detects a stale Blender datablock reference (one of its
        # referenced Objects was removed from the scene). The
        # :class:`SimulationManager` reads this flag and unregisters
        # the module so we don't spam ``update failed`` to the console
        # every tick.
        self._alive: bool = True

    def update(self, dt: float) -> None:
        raise NotImplementedError

    def collision_structure(self):
        """向 :class:`CollisionEngine` 声明本模块的 3D 结构(碰撞契约)。

        返回 :class:`collision_structure.CollisionStructure`:

        - ``members``:属于本结构的对象(host + 静态部件 + 声明的工件)。
          **组内任意两个对象永不做碰撞检测** —— 即"一个对象永远不和
          自己的子对象碰撞"。
        - ``bodies``:需要被检测的运动体(LinearAxis 的 slider、
          RotateAxis 的 rotator、Cylinder 的 work_bar)。不产生独立
          运动的模块(如真空吸嘴)返回空元组。

        子树展开与"其它模块 host"剪枝由 engine 统一做,模块只列
        **直接成员**。

        默认实现走旧字段名单推断(``legacy_structure``),因此未覆写
        本方法的模块(含离线测试 mock)行为与重构前一致。
        """
        try:
            from .modules.components.collision_structure import (
                legacy_structure,
            )
        except ImportError:  # pragma: no cover - 离线/直接脚本路径
            from modules.components.collision_structure import (  # type: ignore
                legacy_structure,
            )
        return legacy_structure(self)

    def on_collision_hit(self, detail: dict) -> None:
        """engine 判定本模块的运动体发生碰撞时调用(默认 no-op)。

        ``detail`` 是 ``{"axis_id", "obstacle_name", "slider_name",
        "body_name", "kind"}`` 字典(``kind`` ∈ ``"obstacle"`` /
        ``"group"``)。实现方应该在这里把自己的状态机推进 BLOCKED。
        """

    def collision_bodies_active(self) -> bool:
        """本 tick 的运动体是否参与碰撞检测(默认 ``True``)。

        集中式拉模式下,engine 每 tick 检查所有模块的运动体。但重构前各
        模块的"检测时机"并不一致:LinearAxis / RotateAxis 每 tick 都查,
        Cylinder 只在有运动指令时查(`target is None` 就早退)。

        保留这个差异是有意的:停在目标位置(或停在障碍里停车检查)时
        不该因为历史几何穿插被重新锁进 BLOCKED。需要统一"永远检测"
        的模块直接返回 ``True``(默认)即可。
        """
        return True

    def on_collision_cleared(self) -> None:
        """全局碰撞解除 / 碰撞检测被禁用时调用(默认 no-op)。

        实现方应该在这里把自己的状态机从 BLOCKED 里放出来。
        """

    def apply_command(self, cmd: SimulationCommand) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError


def is_object_alive(obj) -> bool:
    """Return True iff ``obj`` is still a valid Blender Object reference.

    Touching any property of a removed Blender datablock raises
    ``ReferenceError: StructRNA of type Object has been removed``.
    The cheapest reliable probe is ``obj.name`` — it touches the
    datablock just enough to expose the stale-reference error if the
    underlying object was deleted, and is harmless for offline mocks
    (which expose ``name`` as a regular attribute).
    """
    if obj is None:
        return False
    try:
        _ = obj.name
        return True
    except ReferenceError:
        return False
    except Exception:
        # Anything else (offline mock, attribute missing) — assume
        # alive so we don't accidentally disable the runtime on
        # benign test fixtures.
        return True