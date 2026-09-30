# -*- coding: utf-8 -*-
"""depsgraph_update_post 钩子:LinearAxis 几何缓存的自动失效。

背景
----
:class:`RailComponent` 与 :class:`SliderComponent` 在轴发现时一次算好
``rail.direction`` / ``rail.center`` / ``x_min`` / ``x_max`` / ``slider._start_position``
并缓存到 ``__slots__``。之后如果 host 空对象或其父级链被移动/旋转
(典型场景: ``LinearAxisX`` 是 ``Slide.Y`` 的孩子 — Slide.Y 被美术拖
到新位置后, ``Rail.X`` 的世界 AABB 朝向改变,但缓存的方向没刷新),
slider 会沿错误世界方向走,产生“linear Axis X 的运动方向不是沿着 rail
的运动方向”。

修法
----
挂一个 ``bpy.app.handlers.depsgraph_update_post`` 处理器,在每次
depsgraph 求值后扫一遍注册的 LinearAxis:比对 host 与 rail 的
``matrix_world`` (translation + 3x3 basis, 12 floats 指纹) 与上次
快照,不一致就调 :meth:`LinearAxis.refresh_geometry_caches`。

为什么不挂 ``on_pre``
- ``on_pre`` 里 ``matrix_world`` 还没更新,读到的是上一帧的值,
  容易漏掉当前帧的变化。

为什么不用 ``depsgraph.id_eval_updated``
- id_eval_updated 给出的是 ID 列表,要从 ID 反查到 host/rail 需要
  遍历场景,不比直接读 ``matrix_world`` 更便宜,而且 bpy API 不稳定。
- 12 floats 指纹是 O(1) 比对,纯 Python 列表解析只要几十纳秒。

为什么只盯 host + rail 的 matrix,不盯 slider
- slider 是 host / rail 的兄弟节点 (同父),不是它们的祖先。
  slider 自身运动不会改 host/rail 的 matrix,因此即使 slider 在动,
  指纹不变 → 不会重复触发 refresh。
- 如果美术手动改了 slider 的 local 位置,本钩子不会触发;那属于
  另一个问题 (slider 自己被搬离原点),与本 issue 无关。

性能
----
每 tick:N 条 LinearAxis × (1 个 host matrix + 1 个 rail matrix) 的
12 floats tuple 比对 + 偶尔一次 refresh。即使 50 条轴,合计开销 < 1ms。

公共 API
--------
- :func:`register(manager)` — 安装 depsgraph_update_post 钩子。
- :func:`unregister()` — 移除钩子。多次调用安全。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple


try:  # Blender 环境
    from bpy.app.handlers import persistent as _persistent
except Exception:  # pragma: no cover — 离线测试无 bpy
    def _persistent(fn):  # type: ignore
        return fn


# 模块状态:handler 已安装 / manager 引用。register/unregister 必须成对。
_HANDLER_INSTALLED = False
_MANAGER_REF: Any = None


def _matrix_signature(obj) -> Optional[Tuple[float, ...]]:
    """把 ``obj.matrix_world`` 压成 12 floats 的 hashable tuple。

    比对 ``(translation, 3x3 basis)``,rotation + translation + scale
    全覆盖;scale 不影响 axis 方向但会让 envelope 拉长,所以也一并
    监听。

    Returns ``None`` 表示 obj 不可用 (被删、没注册),上层跳过。
    """
    if obj is None:
        return None
    try:
        mw = obj.matrix_world
        t = mw.to_translation()
        b = mw.to_3x3()
    except Exception:
        return None
    try:
        # round 到 6 位小数:Blender 在 obj.matrix_world 链计算中会引入
        # 亚毫米级浮点噪声,不取整会每 tick 都判“不一致”,白白刷新。
        return (
            round(float(t[0]), 6),
            round(float(t[1]), 6),
            round(float(t[2]), 6),
            round(float(b[0][0]), 6),
            round(float(b[0][1]), 6),
            round(float(b[0][2]), 6),
            round(float(b[1][0]), 6),
            round(float(b[1][1]), 6),
            round(float(b[1][2]), 6),
            round(float(b[2][0]), 6),
            round(float(b[2][1]), 6),
            round(float(b[2][2]), 6),
        )
    except Exception:
        return None


@_persistent
def _on_depsgraph_update_post(scene, depsgraph=None) -> None:
    """扫描所有 linear_axis 模块,刷新矩阵指纹不一致的轴。

    钩子由 :func:`register` 安装,handler 在 depsgraph_update_post 链
    末尾被调用 (``bpy.app.timers`` 不会调度这个 callback,只能挂 handler)。
    """
    if _MANAGER_REF is None:
        return
    try:
        all_modules = list(_MANAGER_REF.all())
    except Exception:
        return
    for axis in all_modules:
        if getattr(axis, "kind", None) != "linear_axis":
            continue
        if not getattr(axis, "_alive", True):
            continue
        # 没装 rail 的轴(老给口测试替身)直接跳过,避免抛异常。
        rail_obj = getattr(getattr(axis, "rail", None), "obj", None)
        host_obj = getattr(axis, "host_obj", None)
        host_sig = _matrix_signature(host_obj)
        rail_sig = _matrix_signature(rail_obj)
        prev = getattr(axis, "_geometry_cache_hash", None)
        if prev is None:
            # 首次观察:只盖章不刷新,避免“初始化后立刻无意义刷新一次”。
            axis._geometry_cache_hash = (host_sig, rail_sig)
            continue
        if (host_sig, rail_sig) != prev:
            try:
                axis.refresh_geometry_caches()
            except Exception as exc:
                # 任何一侧抛异常都不能让 depsgraph handler 退出。
                print(
                    f"[MotionSimulation][LinearAxis][{getattr(axis, 'axis_id', '?')}] "
                    f"auto-refresh failed: {exc!r}"
                )
            # 即使刷新失败也要更新指纹——否则下一 tick 会一直重试同一组坏
            # 数据,刷屏。这里用 (host_sig, rail_sig) 而不是 old prev,
            # 避免把 stale 状态一直保留在轴里。
            axis._geometry_cache_hash = (host_sig, rail_sig)


def register(manager) -> bool:
    """安装 depsgraph_update_post 钩子。

    Parameters
    ----------
    manager : :class:`SimulationManager`
        提供 ``all()`` 入口读取已注册模块名册。install 后由钩子闭包
        持有引用;unregister 时清空。

    Returns
    -------
    bool
        ``True`` 表示本次调用真正装上了钩子;``False`` 表示已经装过
        (幂等返回)。
    """
    global _HANDLER_INSTALLED, _MANAGER_REF
    if _HANDLER_INSTALLED:
        return False
    try:
        import bpy  # noqa: F401
    except Exception:
        return False
    try:
        import bpy as _bpy
        if _on_depsgraph_update_post not in _bpy.app.handlers.depsgraph_update_post:
            _bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update_post)
    except Exception as exc:
        print(f"[MotionSimulation] geometry_watch register failed: {exc!r}")
        return False
    _MANAGER_REF = manager
    _HANDLER_INSTALLED = True
    return True


def unregister() -> bool:
    """移除 depsgraph_update_post 钩子。

    Returns ``True`` 表示本次真正移除了一条 handler;``False`` 表示
    本来就没装 (幂等返回)。unregister 后 manager 引用清空,下次
    register 需要重新传入。
    """
    global _HANDLER_INSTALLED, _MANAGER_REF
    if not _HANDLER_INSTALLED:
        return False
    try:
        import bpy as _bpy
        while _on_depsgraph_update_post in _bpy.app.handlers.depsgraph_update_post:
            _bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update_post)
    except Exception as exc:
        print(f"[MotionSimulation] geometry_watch unregister failed: {exc!r}")
    _MANAGER_REF = None
    _HANDLER_INSTALLED = False
    return True


__all__ = ["register", "unregister", "_matrix_signature", "_on_depsgraph_update_post"]