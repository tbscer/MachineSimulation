#define test1

using System;
using System.Collections.ObjectModel;
using System.Threading;
using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Twin.Application.MotionModules;
using Twin.MotionInterface;
using Twin.MotionSimulate;

namespace Twin.Application.ViewModels;

/// <summary>
/// Top-level ViewModel for <c>MainWindow</c>. Owns the <see cref="IMotionCard"/>
/// model, configures the scene, exposes observable state for bindings, and
/// drives the connection lifecycle + collision toggle via RelayCommands.
///
/// Threading: constructed on the UI thread (XAML <c>DataContext</c>),
/// captures <see cref="SynchronizationContext.Current"/> and posts every
/// model-event update through it.
/// </summary>
public partial class MainWindowViewModel : ObservableObject
{
    private readonly IMotionCard _card;
    private readonly SynchronizationContext _ui;

#if test1
    private readonly BondHead _bondheadLeft;
    private readonly BondHead _bondheadRight;

    private readonly Flipper _flipperLeft;

    private readonly Flipper _flipperRight;

    private readonly WaferTable _waferTable;

    public SimConveyor[] Conveyors { get; private set; } = Array.Empty<SimConveyor>();

    /// <summary>取放料循环（4 个 Task + 公共互斥量 M）编排器。</summary>
    private readonly PickPlaceCycle _pickPlaceCycle;

    // ---- per-module Home in-progress flags (UI thread only) ----
    [ObservableProperty] private bool _isBondHeadLeftHoming;
    [ObservableProperty] private bool _isBondHeadRightHoming;
    [ObservableProperty] private bool _isFlipperLeftHoming;
    [ObservableProperty] private bool _isFlipperRightHoming;

    // ---- one-key auto-home sequence in-progress flag (UI thread only) ----
    [ObservableProperty] private bool _isAutoHoming;

    // ---- pick & place cycle in-progress flag (UI thread only) ----
    [ObservableProperty] private bool _isCycleRunning;
#endif

    /// <summary>
    /// Set while mirroring server-pushed <see cref="IsCollisionEnabled"/>
    /// back into the property — prevents the partial-method callback from
    /// re-issuing <see cref="IMotionCard.UseCollision"/>.
    /// </summary>
    private bool _suppressCollisionSideEffects;

    // ---- connection / status ----
    [ObservableProperty] private string _status = "未连接";
    [ObservableProperty] private bool _isConnected;

    // ---- collision ----
    [ObservableProperty] private bool _isCollisionEnabled;
    [ObservableProperty] private bool _isCollide;
    [ObservableProperty] private string _collisionDetail = "点击 CheckBox 切换；服务器将在下一帧 state_push 中确认状态。";

    // ---- log ----
    [ObservableProperty] private string _log = string.Empty;

    // ---- collections ----
    public ObservableCollection<AxisViewModel> Axes { get; } = new();
    public ObservableCollection<CylinderViewModel> Cylinders { get; } = new();
    public ObservableCollection<InputViewModel> Inputs { get; } = new();
    public ObservableCollection<OutputViewModel> Outputs { get; } = new();
    public ObservableCollection<VacuumNozzleViewModel> VacuumNozzles { get; } = new();

    public MainWindowViewModel() : this(new SimMotionCard()) { }

    public MainWindowViewModel(IMotionCard card)
    {
        _card = card ?? throw new ArgumentNullException(nameof(card));
        _ui = SynchronizationContext.Current
              ?? throw new InvalidOperationException(
                  "MainWindowViewModel must be constructed on the UI thread.");

        if (card is SimMotionCard sim)
        {
            sim.StateIntervalMs = 200;
            sim.AutoReconnect   = true;
            ConfigureScene(sim);

#if test1
            Conveyors = new[]
            {
                sim.AddConveyor("Conveyor1"),
                sim.AddConveyor("Conveyor2"),
                sim.AddConveyor("Conveyor3"),
            };

            _bondheadLeft = new BondHead(isLeft: true);
            _bondheadRight = new BondHead(isLeft: false);
            _flipperLeft = new Flipper(isLeft: true);
            _flipperRight = new Flipper(isLeft: false);

            _waferTable = new WaferTable();

            _bondheadLeft.Init(sim);
            _bondheadRight.Init(sim);
            _flipperLeft.Init(sim);
            _flipperRight.Init(sim);
            _waferTable.Init(sim);

            _pickPlaceCycle = new PickPlaceCycle(
                _bondheadLeft, _bondheadRight, _flipperLeft, _flipperRight, _waferTable, AppendLog);
            _pickPlaceCycle.CycleEnded += OnCycleEnded;
#endif
        }

        BuildChildViewModels(card);
        SubscribeToCard(card);
    }

    private static void ConfigureScene(SimMotionCard sim)
    {
#if test1
        sim.AddAxis("LinearAxisX", -1, MotionKind.Linear);
        sim.AddAxis("LinearAxisY", -1, MotionKind.Linear);
        sim.AddAxis("LinearAxisZ", 1, MotionKind.Linear);
        sim.AddAxis("RotateAxis.R", -1, MotionKind.Rotary);

        sim.AddAxis("LinearAxisX2", -1, MotionKind.Linear);
        sim.AddAxis("LinearAxisY2", 1, MotionKind.Linear);
        sim.AddAxis("LinearAxisZ2", 1, MotionKind.Linear);
        sim.AddAxis("RotateAxis.R2", -1, MotionKind.Rotary);


        sim.AddAxis("LinearAxis.Rotate.LZ", -1, MotionKind.Linear);
        sim.AddAxis("RotateAxis.LZ", -1, MotionKind.Rotary);

        sim.AddAxis("LinearAxis.Rotate.RZ", -1, MotionKind.Linear);
        sim.AddAxis("RotateAxis.RZ", 1, MotionKind.Rotary);

      
        sim.AddVacuumNozzle("VacuumNozzle.LZ");
        sim.AddVacuumNozzle("VacuumNozzle.RZ");


        sim.AddAxis("LinearAxis.WX", -1, MotionKind.Linear);
        sim.AddAxis("LinearAxis.WY", -1, MotionKind.Linear);


        sim.AddAxis("LinearAxis.NZ", -1, MotionKind.Linear);

#else
        sim.AddAxis("LinearAxisX", 1, MotionKind.Linear);
        sim.AddCylinder("Cylinder1");
        sim.AddVacuumNozzle("VacuumNozzle1");
#endif

    }

    private void BuildChildViewModels(IMotionCard card)
    {
        foreach (var a in card.Axis.Values)      Axes.Add(new AxisViewModel(a));
        foreach (var c in card.Cylinders.Values) Cylinders.Add(new CylinderViewModel(c));
        foreach (var i in card.Inputs.Values)    Inputs.Add(new InputViewModel(i));
        foreach (var o in card.Outputs.Values)   Outputs.Add(new OutputViewModel(o));
        foreach (var v in card.VacuumNozzles.Values) VacuumNozzles.Add(new VacuumNozzleViewModel(v));
    }

    private void SubscribeToCard(IMotionCard card)
    {
        card.StateUpdated     += OnCardStateUpdated;
        card.ConnectionChanged += OnCardConnectionChanged;
    }

    private void OnCardStateUpdated()
    {
        // StateUpdated fires on the RPC read-loop thread.
        _ui.Post(_ =>
        {
            // Suppress the [ObservableProperty] partial-method callback so the
            // server-pushed `enabled` doesn't get echoed back as a UseCollision
            // command. (TwoWay binding from the CheckBox still triggers the
            // command — those run on the UI thread without the flag set.)
            _suppressCollisionSideEffects = true;
            try
            {
                IsCollisionEnabled = _card.IsCollisionEnabled;
                IsCollide          = _card.IsCollide;
                UpdateCollisionDetail();
            }
            finally { _suppressCollisionSideEffects = false; }
        }, null);
    }

    private void OnCardConnectionChanged(bool connected)
    {
        _ui.Post(_ =>
        {
            IsConnected = connected;
            Status = connected ? "已连接" : "未连接";
            AppendLog(connected ? "已连接" : "连接已断开（自动重连中…）");
        }, null);
    }

    private void UpdateCollisionDetail()
    {
        if (IsCollide)
            CollisionDetail = "服务器报告至少一对物体正在相交。";
        else if (IsCollisionEnabled)
            CollisionDetail = "服务器已开启碰撞检测，当前无碰撞。";
        else
            CollisionDetail = "服务器未开启碰撞检测；物体会彼此穿透。";
    }

    // ---- two-way binding callback (UI thread, not suppressed) ----

    partial void OnIsCollisionEnabledChanged(bool value)
    {
        if (_suppressCollisionSideEffects) return;
        _card.UseCollision(value);
        AppendLog($"已发送 {(value ? "enable" : "disable")}_collision_detection");
        CollisionDetail = $"本地意图：{(value ? "启用" : "禁用")} (等待服务器下一帧 push 确认)";
    }

    // ---- commands ----

    [RelayCommand(CanExecute = nameof(CanConnect))]
    private async Task ConnectAsync()
    {
        try
        {
            var ok = await _card.ConnectAsync("127.0.0.1", 9877);
            if (!ok) AppendLog("连接失败：服务不可达或超时（mock 在 .tmp/mock_blender_server.py）");
        }
        catch (Exception ex) { AppendLog($"连接异常: {ex.Message}"); }
    }
    private bool CanConnect() => !IsConnected;

    [RelayCommand(CanExecute = nameof(CanDisconnect))]
    private async Task DisconnectAsync()
    {
        try { await _card.DisconnectAsync(); }
        catch (Exception ex) { AppendLog($"断开异常: {ex.Message}"); }
    }
    private bool CanDisconnect() => IsConnected;

    // ---- MotionModule Home commands ----
    // Each MotionModule.Home() orchestrates a multi-axis sequence (Z first,
    // then X/Y/R in parallel for BondHead; Z then R for Flipper). The commands
    // gate on the corresponding IsHoming flag so the user can't re-trigger
    // while a sequence is still running, and they log start/finish/error to
    // the shared log box.

#if test1
    [RelayCommand(CanExecute = nameof(CanHomeBondHeadLeft))]
    private async Task HomeBondHeadLeftAsync()
    {
        IsBondHeadLeftHoming = true;
        AppendLog("BondHead-Left: Home 开始...");
        try
        {
            await _bondheadLeft.Home().ConfigureAwait(true);
            AppendLog("BondHead-Left: Home 完成");
        }
        catch (Exception ex) { AppendLog($"BondHead-Left: Home 异常 — {ex.Message}"); }
        finally { IsBondHeadLeftHoming = false; }
    }
    private bool CanHomeBondHeadLeft() => !IsBondHeadLeftHoming && !IsAutoHoming && !IsCycleRunning;

    [RelayCommand(CanExecute = nameof(CanHomeBondHeadRight))]
    private async Task HomeBondHeadRightAsync()
    {
        IsBondHeadRightHoming = true;
        AppendLog("BondHead-Right: Home 开始...");
        try
        {
            await _bondheadRight.Home().ConfigureAwait(true);
            AppendLog("BondHead-Right: Home 完成");
        }
        catch (Exception ex) { AppendLog($"BondHead-Right: Home 异常 — {ex.Message}"); }
        finally { IsBondHeadRightHoming = false; }
    }
    private bool CanHomeBondHeadRight() => !IsBondHeadRightHoming && !IsAutoHoming && !IsCycleRunning;

    [RelayCommand(CanExecute = nameof(CanHomeFlipperLeft))]
    private async Task HomeFlipperLeftAsync()
    {
        IsFlipperLeftHoming = true;
        AppendLog("Flipper-Left: Home 开始...");
        try
        {
            await _flipperLeft.Home().ConfigureAwait(true);
            AppendLog("Flipper-Left: Home 完成");
        }
        catch (Exception ex) { AppendLog($"Flipper-Left: Home 异常 — {ex.Message}"); }
        finally { IsFlipperLeftHoming = false; }
    }
    private bool CanHomeFlipperLeft() => !IsFlipperLeftHoming && !IsAutoHoming && !IsCycleRunning;

    [RelayCommand(CanExecute = nameof(CanHomeFlipperRight))]
    private async Task HomeFlipperRightAsync()
    {
        IsFlipperRightHoming = true;
        AppendLog("Flipper-Right: Home 开始...");
        try
        {
            await _flipperRight.Home().ConfigureAwait(true);
            AppendLog("Flipper-Right: Home 完成");
        }
        catch (Exception ex) { AppendLog($"Flipper-Right: Home 异常 — {ex.Message}"); }
        finally { IsFlipperRightHoming = false; }
    }
    private bool CanHomeFlipperRight() => !IsFlipperRightHoming && !IsAutoHoming && !IsCycleRunning;

    // ---- one-key auto-home sequence ----
    // 一键回零：
    //   1) 两个 BondHead 的 Z 轴并行回零
    //   2) 两个 BondHead 的 Y 轴并行点动到限位（左正限位 / 右负限位）
    //   3) 左 BondHead Y 回零完成后，再让右 BondHead Y 回零（串行）
    //   4) 两个 BondHead 的 X/R 回零与两个 Flipper 的全部回零并行执行
    //   5) 两个 Flipper 回零完成后，WaferTable 回零
    // 步骤 1/2 失败会中止整个序列（Z 未回零或 Y 未到限位时继续动作有撞机风险）；
    // 步骤 3/4 失败只记录日志并继续，最终汇总结果。
    [RelayCommand(CanExecute = nameof(CanAutoHomeAll))]
    private async Task AutoHomeAllAsync()
    {
        IsAutoHoming = true;
        AppendLog("一键回零：开始");
        bool ok = true;
        try
        {
            // 1) Z 轴先回零，串行后续动作的前提。
            AppendLog("一键回零 [1/4]：BondHead Z 回零 ...");
            bool[] z = await Task.WhenAll(_bondheadLeft.HomeZ(), _bondheadRight.HomeZ());
            AppendLog($"一键回零：Z 回零 左={z[0]} 右={z[1]}");
            if (!z[0] || !z[1])
            {
                AppendLog("一键回零：Z 回零失败，序列中止");
                return;
            }

            // 2) Y 轴点动到限位（左边正限位，右边负限位），并行。
            AppendLog("一键回零 [2/4]：BondHead Y 点动到限位 ...");
            bool[] jog = await Task.WhenAll(_bondheadLeft.JogLimitY(), _bondheadRight.JogLimitY());
            AppendLog($"一键回零：Y 限位点动 左={jog[0]} 右={jog[1]}");
            if (!jog[0] || !jog[1])
            {
                AppendLog("一键回零：Y 限位点动失败，序列中止");
                return;
            }

            // 3) Y 轴回零：左完成后才启动右（串行）。
            AppendLog("一键回零 [3/4]：BondHead Y 回零（先左后右）...");
            bool yLeft = await _bondheadLeft.HomeY();
            AppendLog($"一键回零：左 BondHead Y 回零 {(yLeft ? "完成" : "失败")}");
            bool yRight = await _bondheadRight.HomeY();
            AppendLog($"一键回零：右 BondHead Y 回零 {(yRight ? "完成" : "失败")}");
            ok &= yLeft && yRight;

            // 4) BondHead 的 X/R 回零与两个 Flipper 的全部回零并行。
            AppendLog("一键回零 [4/4]：BondHead X/R 回零 + Flipper 回零（并行）...");
            Task<bool> xrLeft = _bondheadLeft.HomeXR();
            Task<bool> xrRight = _bondheadRight.HomeXR();
            Task flipLeft = _flipperLeft.Home();
            Task flipRight = _flipperRight.Home();
            await Task.WhenAll(xrLeft, xrRight, flipLeft, flipRight);
            AppendLog($"一键回零：BondHead X/R 回零 左={xrLeft.Result} 右={xrRight.Result}");
            ok &= xrLeft.Result && xrRight.Result;

            // 5) 两个 Flipper 回零完成后，再执行 WaferTable 回零。
            AppendLog("一键回零 [5/5]：WaferTable 回零...");
            await _waferTable.Home();
            AppendLog("一键回零：WaferTable 回零完成");

            AppendLog(ok ? "一键回零：全部完成" : "一键回零：结束，但存在失败项（见上方日志）");
        }
        catch (Exception ex) { AppendLog($"一键回零异常 — {ex.Message}"); }
        finally { IsAutoHoming = false; }
    }
    private bool CanAutoHomeAll() =>
        !IsAutoHoming && !IsCycleRunning &&
        !IsBondHeadLeftHoming && !IsBondHeadRightHoming &&
        !IsFlipperLeftHoming && !IsFlipperRightHoming;

    // ---- 取放料循环：WaferTable 准备完成后启动 4 个 Task ----
    // T1/T2：XYZR 回 0 → 等翻转 picked done → 放置点(Z 下压 200ms) → 置 wait pick
    //         → 取料点(Z 下压 200ms) → 退回 0 → 循环。
    // T3/T4：Z/R 回 0 → 置 wait pick → 取公共互斥量 M → R(±180)/Z-30(保持 100ms)/回 0
    //         → 释放 M → 置 picked done → 等 wait pick → 循环。
    // 说明：循环在后台长期运行，日志只记录启动/停止/异常，避免刷屏。
    [RelayCommand(CanExecute = nameof(CanStartPickPlace))]
    private void StartPickPlace()
    {
        if (_card.IsCollide)
            AppendLog("提示：当前处于碰撞锁定状态，运动命令会被服务器拒绝；可在「碰撞」页关闭再重新启用碰撞检测以清除锁定。");

        _pickPlaceCycle.Start();
        IsCycleRunning = _pickPlaceCycle.IsRunning;
    }
    private bool CanStartPickPlace() =>
        !IsCycleRunning && !IsAutoHoming &&
        !IsBondHeadLeftHoming && !IsBondHeadRightHoming &&
        !IsFlipperLeftHoming && !IsFlipperRightHoming;

    [RelayCommand(CanExecute = nameof(CanStopPickPlace))]
    private async Task StopPickPlaceAsync()
    {
        try
        {
            await _pickPlaceCycle.StopAsync().ConfigureAwait(true);
        }
        catch (Exception ex) { AppendLog($"取放料循环停止异常 — {ex.Message}"); }
        finally { IsCycleRunning = false; }
    }
    private bool CanStopPickPlace() => IsCycleRunning;

    /// <summary>
    /// 循环的 4 个 Task 已全部退出（用户停止或异常中止）。在后台线程触发，
    /// 这里只负责把「循环运行中」状态复位（结束语已由 PickPlaceCycle 写日志）。
    /// </summary>
    private void OnCycleEnded(string message)
        => _ui.Post(_ => IsCycleRunning = false, null);

    // Partial-method callbacks from [ObservableProperty] — ask the command
    // system to re-evaluate CanExecute so the button enables/disables in
    // lock-step with the IsHoming flag toggled above.
    partial void OnIsBondHeadLeftHomingChanged(bool value)
        => HomeBondHeadLeftCommand.NotifyCanExecuteChanged();
    partial void OnIsBondHeadRightHomingChanged(bool value)
        => HomeBondHeadRightCommand.NotifyCanExecuteChanged();
    partial void OnIsFlipperLeftHomingChanged(bool value)
        => HomeFlipperLeftCommand.NotifyCanExecuteChanged();
    partial void OnIsFlipperRightHomingChanged(bool value)
        => HomeFlipperRightCommand.NotifyCanExecuteChanged();

    // 一键回零运行期间禁用所有单模块 Home 按钮，反之亦然。
    partial void OnIsAutoHomingChanged(bool value)
    {
        AutoHomeAllCommand.NotifyCanExecuteChanged();
        HomeBondHeadLeftCommand.NotifyCanExecuteChanged();
        HomeBondHeadRightCommand.NotifyCanExecuteChanged();
        HomeFlipperLeftCommand.NotifyCanExecuteChanged();
        HomeFlipperRightCommand.NotifyCanExecuteChanged();
        StartPickPlaceCommand.NotifyCanExecuteChanged();
    }

    // 取放料循环运行期间禁用所有 Home / 一键回零，反之亦然。
    partial void OnIsCycleRunningChanged(bool value)
    {
        StartPickPlaceCommand.NotifyCanExecuteChanged();
        StopPickPlaceCommand.NotifyCanExecuteChanged();
        AutoHomeAllCommand.NotifyCanExecuteChanged();
        HomeBondHeadLeftCommand.NotifyCanExecuteChanged();
        HomeBondHeadRightCommand.NotifyCanExecuteChanged();
        HomeFlipperLeftCommand.NotifyCanExecuteChanged();
        HomeFlipperRightCommand.NotifyCanExecuteChanged();
    }
#endif

    // ---- helpers ----

    private void AppendLog(string msg)
    {
        var line = $"[{DateTime.Now:HH:mm:ss}] {msg}\n";
        if (SynchronizationContext.Current == _ui) Log += line;
        else _ui.Post(_ => Log += line, null);
    }

    /// <summary>
    /// Re-evaluate CanExecute on Connect/Disconnect commands whenever IsConnected changes.
    /// </summary>
    partial void OnIsConnectedChanged(bool value)
    {
        ConnectCommand.NotifyCanExecuteChanged();
        DisconnectCommand.NotifyCanExecuteChanged();
    }

    /// <summary>Called from the View on Closed; releases the card.</summary>
    public void DisposeRequested()
    {
#if test1
        // 窗口关闭时取消后台循环（不阻塞关闭流程）。
        if (_pickPlaceCycle.IsRunning) _ = _pickPlaceCycle.StopAsync();
#endif
        _card.Dispose();
    }
}