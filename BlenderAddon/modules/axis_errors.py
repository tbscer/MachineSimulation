# -*- coding: utf-8 -*-
"""axis 模块共用的错误类型。

设计原则
--------

``reset_collision`` **不是**模块的 action —— 它是 collision engine 的
系统级操作(清 scene-level collision marker 并释放所有 BLOCKED 模块)。

调用方如果想解锁被碰撞锁住的 axis,应该调::

    engine.clear_collision()        # 仅清 scene marker
    engine.set_disabled(False)      # 同上 + 强制 release 所有 BLOCKED 模块

这些方法存在于 :class:`CollisionEngine`(见
:mod:`modules.components.collision`),可以经由 Dev panel / RPC server
路径调。模块自己的 ``axis_cmd_*`` 队列里**没有** ``reset_collision``,
也不能接受它(从 ``VALID_ACTIONS`` 里被剔除)。

当 axis 处于 BLOCKED 状态时,``send_command`` 和 ``apply_command``
会抛 :class:`AxisBlockedError`,让 UI operator 报红 + RPC handler
转成专门的 ``AXIS_BLOCKED`` 错误码。调用方根据这个反馈去调
``engine.clear_collision()`` 而不是试图再发 ``reset_collision`` 命令。
"""

from __future__ import annotations


class AxisBlockedError(RuntimeError):
    """发送给 axis 的命令被拒,因为 axis 处于 BLOCKED 状态。

    由 :func:`modules.LinearAxix.axis_ops.send_command`、
    :func:`modules.RotateAxis.axis_ops.send_command`、
    :meth:`BaseSimulationModule.apply_command` 在 ``state == "blocked"``
    且 ``action != "reset_collision"`` 时抛出。

    调用方收到后应该::

        from MotionSimulation.addon import get_manager
        engine = get_manager()._collision_engine
        engine.clear_collision()    # 清 scene marker,下一 tick 自动 release
        # 或更强:engine.set_disabled(False) 也强制 release 当前所有 BLOCKED

    不要试图再 ``send_command("reset_collision", ...)``:它是 engine
    的操作,不是模块的 action —— 会从 ``VALID_ACTIONS`` 里被 rejected。
    """