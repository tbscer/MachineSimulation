using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Twin.MotionInterface;

namespace Twin.Application.MotionModules;

/// <summary>
/// 键合头 (BondHead) 与翻转模组 (Flipper) 之间的握手状态。
/// </summary>
internal enum HandshakeState
{
    /// <summary>等待翻转模组来取料（由键合头在该位置就绪后置位）。</summary>
    WaitPick,

    /// <summary>翻转模组已完成取料（由翻转模组在取料动作结束后置位）。</summary>
    PickedDone,
}

/// <summary>
/// 单边（键合头 ↔ 翻转）握手槽。Set / WaitForAsync 组成一个异步条件变量：
/// 状态被保存下来，因此即使 Set 先于 WaitForAsync 发生也不会丢失通知。
/// </summary>
internal sealed class HandshakeSlot
{
    private readonly object _gate = new();
    private HandshakeState _state = HandshakeState.WaitPick;
    private TaskCompletionSource<bool> _changed = NewTcs();

    private static TaskCompletionSource<bool> NewTcs() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public HandshakeState State { get { lock (_gate) return _state; } }

    /// <summary>复位为初始状态 WaitPick。</summary>
    public void Reset()
    {
        lock (_gate)
        {
            _state = HandshakeState.WaitPick;
        }
    }

    public void Set(HandshakeState state)
    {
        TaskCompletionSource<bool> previous;
        lock (_gate)
        {
            // 状态未变化时无人在等待该状态（WaitForAsync 会立刻返回），无需发信号。
            if (_state == state) return;

            _state = state;
            previous = _changed;
            _changed = NewTcs();
        }
        previous.TrySetResult(true);
    }

    /// <summary>等待直到状态变为 <paramref name="target"/>。可取消。</summary>
    public async Task WaitForAsync(HandshakeState target, CancellationToken ct)
    {
        while (true)
        {
            Task wait;
            lock (_gate)
            {
                if (_state == target) return;
                wait = _changed.Task;
            }
            await wait.WaitAsync(ct).ConfigureAwait(false);
        }
    }
}

/// <summary>
/// 取放料循环编排器：先执行 WaferTable 取料准备，再启动 4 个并发 Task，
/// 按“键合头取放 + 翻转取料”握手循环运行，直到调用 <see cref="StopAsync"/> 或运动失败。
///
/// <list type="bullet">
///   <item>T1 = 左键合头：XYZR 回 0/工作位 → 等左翻转 picked done → (-55, 175, Z-55) 等 200ms → Z0
///         → 置 wait pick → (100, 0, Z-40) 等 200ms → Z0 → 循环</item>
///   <item>T2 = 右键合头：同上，取放点改为 (215, -158)</item>
///   <item>T3 = 左翻转：ZR 回 0 → 置 wait pick → 取 M → R180 / Z-30 → NZ35/30 → 等 100ms → Z0 / R0
///         → 放 M → 置 picked done → 等 wait pick → 循环</item>
///   <item>T4 = 右翻转：同上，R 到 -180</item>
/// </list>
///
/// 公共互斥量 M 由两个翻转共享，用于串行化两侧的取料动作（同一时刻只允许一个翻转取料）。
///
/// 说明：需求描述中 T3/T4 在“标记 picked done”前后各出现了一次“释放互斥量 M”，
/// 这里按 acquire/release 配对实现（取料动作期间持锁，之后释放一次），额外释放会
/// 让 <see cref="SemaphoreSlim"/> 超出上限抛 <see cref="SemaphoreFullException"/>。
///
/// 失败处理：每一步运动都走 <see cref="EnsureAsync"/>，未到位立即中止整个循环并写日志。
/// 这样遇到碰撞锁定 / 到限位 / 命令被拒时，不会让某个 Task 死等几十秒。
///
/// 速度由 <see cref="BondHead.SetWorkSpeed"/> / <see cref="Flipper.SetWorkSpeed"/> 在启动时设置。
/// </summary>
internal sealed class PickPlaceCycle
{
    /// <summary>循环内单次运动的等待上限（ms）；超过即判失败并中止循环。</summary>
    private const int MoveTimeoutMs = 15000;

    private readonly BondHead _leftHead;
    private readonly BondHead _rightHead;
    private readonly Flipper _leftFlip;
    private readonly Flipper _rightFlip;
    private readonly WaferTable _waferTable;
    private readonly Action<string> _log;

    // 键合头 ↔ 翻转 的握手槽（左右各一组）
    private readonly HandshakeSlot _leftSlot = new();
    private readonly HandshakeSlot _rightSlot = new();

    // 公共互斥量 M：左右两个翻转的取料动作互斥。
    private readonly SemaphoreSlim _mutex = new(1, 1);

    private CancellationTokenSource? _cts;
    private Task? _cycleTask;

    /// <summary>中止原因；null / 空表示正常停止。由 Guard 写入，监工 Task 读取。</summary>
    private volatile string? _abortReason;

    /// <summary>
    /// 4 个 Task 全部退出时触发（用户停止或异常中止），参数为写入日志的结语。
    /// 在线程池线程上触发；UI 层负责把「循环运行中」状态复位。
    /// </summary>
    public event Action<string>? CycleEnded;

    public PickPlaceCycle(
        BondHead leftHead,
        BondHead rightHead,
        Flipper leftFlip,
        Flipper rightFlip,
        WaferTable waferTable,
        Action<string> log)
    {
        _leftHead = leftHead;
        _rightHead = rightHead;
        _leftFlip = leftFlip;
        _rightFlip = rightFlip;
        _waferTable = waferTable;
        _log = log;
    }

    public bool IsRunning => _cts is { IsCancellationRequested: false };

    /// <summary>开启 T1~T4 四个 Task 并立即返回（循环在后台运行）。</summary>
    public void Start()
    {
        if (IsRunning || _cycleTask is { IsCompleted: false }) return;

        _leftSlot.Reset();
        _rightSlot.Reset();
        _abortReason = null;

        _leftHead.SetWorkSpeed();
        _rightHead.SetWorkSpeed();
        _leftFlip.SetWorkSpeed();
        _rightFlip.SetWorkSpeed();

        var cts = new CancellationTokenSource();
        _cts = cts;
        var ct = cts.Token;

        _log("取放料循环: 开始 WaferTable 取料准备");
        Task cycleTask = Task.Run(() => RunCycleAsync(cts, ct));
        _cycleTask = cycleTask;

        // 监工 Task：等预处理和循环任务全部退出后通知 UI（用户停止或异常中止），
        // 否则界面会一直停在“循环运行中...”。
        _ = Task.Run(async () =>
        {
            try { await cycleTask.ConfigureAwait(false); }
            catch { /* 已在 Guard 里处理 */ }

            string reason = _abortReason ?? string.Empty;
            string message = reason.Length == 0
                ? "取放料循环: 已停止"
                : $"取放料循环: 已中止 - {reason}";
            _log(message);
            if (ReferenceEquals(_cts, cts)) _cts = null;
            if (ReferenceEquals(_cycleTask, cycleTask)) _cycleTask = null;
            cts.Dispose();
            CycleEnded?.Invoke(message);
        });
    }

    private async Task RunCycleAsync(CancellationTokenSource cts, CancellationToken ct)
    {
        Task Guard(string name, Func<Task> body) => Task.Run(async () =>
        {
            try
            {
                await body().ConfigureAwait(false);
                _log($"{name}: 循环结束");
            }
            catch (OperationCanceledException) { }
            catch (CycleAbortException ex)
            {
                _abortReason = ex.Message;
                cts.Cancel();
            }
            catch (Exception ex)
            {
                _abortReason = $"{name} 异常 - {ex.Message}";
                cts.Cancel();
            }
        }, ct);

        try
        {
            if (!await _waferTable.JogNZToNegLimitAsync(ct).ConfigureAwait(false))
                throw new CycleAbortException("WaferTable NZ 未到达负限位");

            _log("取放料循环: WaferTable NZ 已到负限位，WX/WY 移动到取料点");
            await MoveAsync(ct, (_waferTable.X, 150.0), (_waferTable.Y, -88.0)).ConfigureAwait(false);
            await Task.Delay(500, ct).ConfigureAwait(false);

            _log("取放料循环: WaferTable 取料完成，WX/WY 回零并将 NZ 移至 20");
            await MoveAsync(ct, (_waferTable.X, 0.0), (_waferTable.Y, 0.0)).ConfigureAwait(false);
            await Task.Delay(500, ct).ConfigureAwait(false);
            await MoveAsync(ct, (_waferTable.NZ, 20.0)).ConfigureAwait(false);

            ct.ThrowIfCancellationRequested();
            _log("取放料循环: WaferTable 准备完成，启动 T1(左头) / T2(右头) / T3(左翻转) / T4(右翻转)");

            Task[] tasks =
            {
                Guard("T1", () => LeftHeadAsync(ct)),
                Guard("T2", () => RightHeadAsync(ct)),
                Guard("T3", () => LeftFlipAsync(ct)),
                Guard("T4", () => RightFlipAsync(ct)),
            };
            await Task.WhenAll(tasks).ConfigureAwait(false);
            if (!string.IsNullOrEmpty(_abortReason))
                await StopAllAxesAsync().ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            if (!string.IsNullOrEmpty(_abortReason))
                await StopAllAxesAsync().ConfigureAwait(false);
        }
        catch (CycleAbortException ex)
        {
            _abortReason = ex.Message;
            cts.Cancel();
            await StopAllAxesAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _abortReason = $"取放料循环异常 - {ex.Message}";
            cts.Cancel();
            await StopAllAxesAsync().ConfigureAwait(false);
        }
    }

    /// <summary>取消 4 个 Task、停止所有相关轴，并等待它们退出。</summary>
    public async Task StopAsync()
    {
        var cts = _cts;
        if (cts is null) return;

        _cts = null;
        _log("取放料循环: 停止请求，取消当前动作并停止轴 ...");

        try { cts.Cancel(); }
        catch (ObjectDisposedException) { }

        // MoveToAsync 本身不可取消，靠“停轴 → state=idle”让它提前返回。
        await StopAllAxesAsync().ConfigureAwait(false);

        try
        {
            if (_cycleTask is { } cycleTask)
                await cycleTask.ConfigureAwait(false);
        }
        catch (OperationCanceledException) { }

        cts.Dispose();
        _log("取放料循环: 已停止");
    }

    // ---- T1: 左键合头 ----
    private async Task LeftHeadAsync(CancellationToken ct)
    {
        var head = _leftHead;

        while (!ct.IsCancellationRequested)
        {
            // 1) XYZR 全部回到 0 点
            await MoveAsync(ct,
                (head.X, -40.0), (head.Y, 175.0),
                (head.Z, 0.0), (head.R, 0.0)).ConfigureAwait(false);

            // 2) 等待左翻转完成取料
            await _leftSlot.WaitForAsync(HandshakeState.PickedDone, ct).ConfigureAwait(false);

            // 3) 运动到放置点，Z 下压 200ms 后抬起
            await MoveAsync(ct, (head.X, -55.0), (head.Y, 175.0)).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, -55.0).ConfigureAwait(false);
            await Task.Delay(200, ct).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, 0.0).ConfigureAwait(false);

            // 4) 通知左翻转可以开始下一次取料
            _leftSlot.Set(HandshakeState.WaitPick);

            // 5) 运动到取料点，Z 下压 200ms 后抬起
            await MoveAsync(ct, (head.X, 100.0), (head.Y, 0.0)).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, -40.0).ConfigureAwait(false);
            await Task.Delay(200, ct).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, 0.0).ConfigureAwait(false);
        }
    }

    // ---- T2: 右键合头 ----
    private async Task RightHeadAsync(CancellationToken ct)
    {
        var head = _rightHead;

        while (!ct.IsCancellationRequested)
        {
            // 1) XYZR 全部回到 0 点
            await MoveAsync(ct,
                (head.X, 200.0), (head.Y, -158.0),
                (head.Z, 0.0), (head.R, 0.0)).ConfigureAwait(false);

            // 2) 等待右翻转完成取料
            await _rightSlot.WaitForAsync(HandshakeState.PickedDone, ct).ConfigureAwait(false);

            // 3) 运动到放置点，Z 下压 200ms 后抬起
            await MoveAsync(ct, (head.X, 215.0), (head.Y, -158.0)).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, -55.0).ConfigureAwait(false);
            await Task.Delay(200, ct).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, 0.0).ConfigureAwait(false);

            // 4) 通知右翻转可以开始下一次取料
            _rightSlot.Set(HandshakeState.WaitPick);

            // 5) 运动到取料点，Z 下压 200ms 后抬起
            await MoveAsync(ct, (head.X, 100.0), (head.Y, 0.0)).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, -40.0).ConfigureAwait(false);
            await Task.Delay(200, ct).ConfigureAwait(false);
            await MoveAsync(ct, head.Z, 0.0).ConfigureAwait(false);
        }
    }

    // ---- T3: 左翻转 ----
    private async Task LeftFlipAsync(CancellationToken ct)
    {
        var flip = _leftFlip;

        while (!ct.IsCancellationRequested)
        {
            // 1) Z、R 回到 0 点，并置为等待取料
            await MoveAsync(ct, (flip.Z, 0.0), (flip.R, 0.0)).ConfigureAwait(false);
            _leftSlot.Set(HandshakeState.WaitPick);

            // 2) 取料动作：R 翻转 180°，Z 下探 100ms 后复位（持公共互斥量 M）
            await _mutex.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                await MoveAsync(ct, flip.R, 180.0).ConfigureAwait(false);
                await MoveAsync(ct, flip.Z, -30.0).ConfigureAwait(false);
                await MoveAsync(ct, _waferTable.NZ, 35.0).ConfigureAwait(false);
                await MoveAsync(ct, _waferTable.NZ, 30.0).ConfigureAwait(false);
                await Task.Delay(100, ct).ConfigureAwait(false);
                await MoveAsync(ct, flip.Z, 0.0).ConfigureAwait(false);
                await MoveAsync(ct, flip.R, 0.0).ConfigureAwait(false);
            }
            finally { _mutex.Release(); }

            // 3) 通知左键合头取料完成，等待键合头置 wait pick 后进入下一轮
            _leftSlot.Set(HandshakeState.PickedDone);
            await _leftSlot.WaitForAsync(HandshakeState.WaitPick, ct).ConfigureAwait(false);
        }
    }

    // ---- T4: 右翻转 ----
    private async Task RightFlipAsync(CancellationToken ct)
    {
        var flip = _rightFlip;

        while (!ct.IsCancellationRequested)
        {
            // 1) Z、R 回到 0 点，并置为等待取料
            await MoveAsync(ct, (flip.Z, 0.0), (flip.R, 0.0)).ConfigureAwait(false);
            _rightSlot.Set(HandshakeState.WaitPick);

            // 2) 取料动作：R 反转 -180°，Z 下探 100ms 后复位（持公共互斥量 M）
            await _mutex.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                await MoveAsync(ct, flip.R, -180.0).ConfigureAwait(false);
                await MoveAsync(ct, flip.Z, -30.0).ConfigureAwait(false);
                await MoveAsync(ct, _waferTable.NZ, 35.0).ConfigureAwait(false);
                await MoveAsync(ct, _waferTable.NZ, 30.0).ConfigureAwait(false);
                await Task.Delay(100, ct).ConfigureAwait(false);
                await MoveAsync(ct, flip.Z, 0.0).ConfigureAwait(false);
                await MoveAsync(ct, flip.R, 0.0).ConfigureAwait(false);
            }
            finally { _mutex.Release(); }

            // 3) 通知右键合头取料完成，等待键合头置 wait pick 后进入下一轮
            _rightSlot.Set(HandshakeState.PickedDone);
            await _rightSlot.WaitForAsync(HandshakeState.WaitPick, ct).ConfigureAwait(false);
        }
    }

    private async Task StopAllAxesAsync()
    {
        List<Task> stops = new()
        {
            _leftHead.X.StopAsync(), _leftHead.Y.StopAsync(),
            _leftHead.Z.StopAsync(), _leftHead.R.StopAsync(),

            _rightHead.X.StopAsync(), _rightHead.Y.StopAsync(),
            _rightHead.Z.StopAsync(), _rightHead.R.StopAsync(),

            _leftFlip.Z.StopAsync(), _leftFlip.R.StopAsync(),
            _rightFlip.Z.StopAsync(), _rightFlip.R.StopAsync(),

            _waferTable.X.StopAsync(), _waferTable.Y.StopAsync(), _waferTable.NZ.StopAsync(),
        };

        await Task.WhenAll(stops).ConfigureAwait(false);
    }

    // ---- 运动封装：未到位即中止循环（不再死等超时）----

    /// <summary>单轴运动（失败即抛 <see cref="CycleAbortException"/>）。</summary>
    private static Task MoveAsync(CancellationToken ct, IAxis axis, double target)
        => EnsureAsync(axis, target, ct);

    /// <summary>多轴并行运动（任一根失败即中止循环）。</summary>
    private static async Task MoveAsync(CancellationToken ct, params (IAxis Axis, double Target)[] moves)
    {
        Task[] all = new Task[moves.Length];
        for (int i = 0; i < moves.Length; i++)
            all[i] = EnsureAsync(moves[i].Axis, moves[i].Target, ct);

        await Task.WhenAll(all).ConfigureAwait(false);
    }

    /// <summary>
    /// 执行一次运动，未到位时抛出 <see cref="CycleAbortException"/>（带原因）。
    /// <see cref="IAxis.MoveToAsync"/> 现在会在轴被碰撞锁定 / 到限位 / 命令被拒 /
    /// 超时时尽快返回 false，因此这里不会死等。
    /// </summary>
    private static async Task EnsureAsync(IAxis axis, double target, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();

        if (await axis.MoveToAsync(target, MoveTimeoutMs).ConfigureAwait(false))
        {
            ct.ThrowIfCancellationRequested();
            return;
        }

        ct.ThrowIfCancellationRequested();   // 用户主动停止，不算失败

        string reason = axis.IsBlocked
            ? $"轴 {axis.Name} 被碰撞锁定 (stop_reason={axis.StopReason}) - 到「碰撞」页关闭再重新启用碰撞检测以清除锁定，或修正模型/路径"
            : axis.LastError.Length > 0
                ? $"轴 {axis.Name} -> {target} 命令被拒绝: {axis.LastError}"
                : $"轴 {axis.Name} -> {target} 未在 {MoveTimeoutMs} ms 内到位 (state={axis.State}, stop_reason={axis.StopReason})";

        throw new CycleAbortException(reason);
    }
}

/// <summary>循环内部使用的「中止循环」信号，携带面向用户的失败原因。</summary>
internal sealed class CycleAbortException : Exception
{
    public CycleAbortException(string message) : base(message) { }
}