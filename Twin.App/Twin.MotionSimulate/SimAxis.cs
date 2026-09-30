using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Twin.MotionInterface;

namespace Twin.MotionSimulate;

public sealed class SimAxis : SimObject, IAxis
{
    private readonly CommandSender _send;
    private readonly MotionKind _kind;
    private double _pos;
    private bool _homed;

    /// Last server-reported motion state; see <see cref="State"/>.
    /// Written by the RPC read-loop thread, read from UI / cycle threads.
    private volatile string _state = "idle";

    private double _speed = 100.0;

    public double Pos => _pos;
    public bool Homed => _homed;

    public string State => _state;

    /// <summary>
    /// True only while the server reports an in-progress motion. Anything else
    /// (``idle`` / ``stopped_at_limit`` / ``blocked``) means the motion has ended —
    /// important because a collision-blocked axis stops mid-path and never becomes
    /// idle; waiting for "idle" there would burn the whole timeout.
    /// </summary>
    public bool IsMoving => _state is "moving_p2p" or "moving_vel" or "homing";

    public bool IsBlocked => _state == "blocked";

    public string StopReason { get; private set; } = string.Empty;

    public string LastError { get; private set; } = string.Empty;
    public double Speed
    {
        get => _speed;
        set => _speed = value;
    }

    public int HomeDir { get; }

    public event Action<double>? PositionChanged;
    public event Action<bool>? HomedChanged;

    public IInputIO HomeSensor { get; }
    public IInputIO PosLimitSensor { get; }
    public IInputIO NegLimitSensor { get; }

    /// Maximum time HomeMotionAsync / MoveToAsync will wait for completion before
    /// returning false / returning silently.
    public int TimeoutMs { get; set; } = 50000;
    internal SimAxis(string name, MotionKind kind, CommandSender send, int homeDir) : base(name)
    {
        _kind = kind;
        _send = send;
        HomeDir = homeDir;
        HomeSensor     = new SimInputIO($"{name}_home");
        PosLimitSensor = new SimInputIO($"{name}_pos");
        NegLimitSensor = new SimInputIO($"{name}_neg");
    }

    public bool ServoOn() => true;

    public override void ApplyState(JsonElement record)
    {
        if (record.TryGetProperty(_kind == MotionKind.Linear ? "current_x" : "current_angle", out var p))
        {
            double newPos = p.GetDouble();
            if (newPos != _pos)
            {
                _pos = newPos;
                PositionChanged?.Invoke(newPos);
            }
        }
        if (record.TryGetProperty("home_done", out var h))
        {
            bool newHomed = h.GetBoolean();
            if (newHomed != _homed)
            {
                _homed = newHomed;
                HomedChanged?.Invoke(newHomed);
            }
        }
        if (record.TryGetProperty("stop_reason", out var reason) && reason.ValueKind == JsonValueKind.String)
            StopReason = reason.GetString() ?? string.Empty;

        if (record.TryGetProperty("state", out var state) && state.ValueKind == JsonValueKind.String)
            _state = state.GetString() ?? string.Empty;
        
        // Tolerant: inline sensor fields within an axis record.
        if (record.TryGetProperty("home_sensor", out var hs))
            ((SimInputIO)HomeSensor).SetFromOwner(hs.GetBoolean());
        if (record.TryGetProperty("pos_limit_sensor", out var ps))
            ((SimInputIO)PosLimitSensor).SetFromOwner(ps.GetBoolean());
        if (record.TryGetProperty("neg_limit_sensor", out var ns))
            ((SimInputIO)NegLimitSensor).SetFromOwner(ns.GetBoolean());
    }

    public async Task<bool> HomeMotionAsync()
    {
        LastError = string.Empty;
        try
        {

            await _send(Name, "home", new Dictionary<string, object>
            {
                ["velocity"] = _speed,
                ["direction"] = HomeDir,
            }).ConfigureAwait(false);

            await Task.Delay(100);

            _homed = false;
        }
        catch (Exception ex)
        {
            LastError = ex.Message;
            return false;
        }

        bool moved = false;
        await WaitForAsync(() =>
        {
            if (_homed || HomeSensor.State) return true;
            if (IsBlocked) return true;              // locked by collision: give up at once
            if (IsMoving) { moved = true; return false; }
            return moved;                            // terminal state after we saw it move
        }, TimeoutMs).ConfigureAwait(false);

        if (IsBlocked && LastError.Length == 0)
            LastError = $"blocked (stop_reason={StopReason})";

        return _homed || HomeSensor.State;
    }

    public async Task<bool> MoveToAsync(double destination, int? timeoutMs = null)
    {
        string targetKey = _kind == MotionKind.Rotary ? "target_angle" : "target_x";
        LastError = string.Empty;

        try
        {
            await _send(Name, "move_to", new Dictionary<string, object>
            {
                [targetKey] = destination,
                ["velocity"] = _speed,
            }).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            // Server refuses commands while the axis is collision-blocked (AXIS_BLOCKED).
            LastError = ex.Message;
            return false;
        }

        // Wait for the destination, or for the motion to end without reaching it.
        // A cancelled/blocked/limit-stopped motion reports a non-moving state; it must
        // end the wait immediately instead of burning the full timeout.
        bool moved = false;
        await WaitForAsync(() =>
        {
            if (Math.Abs(_pos - destination) < 0.001) return true;
            if (IsBlocked) return true;
            if (IsMoving) { moved = true; return false; }
            return moved;
        }, timeoutMs ?? TimeoutMs).ConfigureAwait(false);

        if (IsBlocked && LastError.Length == 0)
            LastError = $"blocked (stop_reason={StopReason})";

        return Math.Abs(_pos - destination) < 0.001;
    }

    public async Task JogAsync(int dir)
    {
        try
        {
            await _send(Name, "jog", new Dictionary<string, object>
            {
                ["direction"] = dir,
                ["velocity"] = _speed,
            }).ConfigureAwait(false);
        }
        catch
        {
            // Swallow — disconnect will surface via ConnectionChanged.
        }
    }

    public async Task StopAsync()
    {
        try
        {
            await _send(Name, "stop", null).ConfigureAwait(false);
        }
        catch
        {
            // Swallow — disconnect will surface via ConnectionChanged.
        }
    }

    private static async Task<bool> WaitForAsync(Func<bool> condition, int timeoutMs)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        while (!condition())
        {
            if (Environment.TickCount64 >= deadline) return false;
            await Task.Delay(50).ConfigureAwait(false);
        }
        return true;
    }
}

public class SimConveyor : SimObject, IAxis
{
    private readonly CommandSender _send;

    private volatile string _state = "idle";

    private int _homeDir;

    private int _speed = 100;

    public SimConveyor(string name, CommandSender send, int homeDir) : base(name)
    {
        _send = send;
        _homeDir = homeDir;
    }

    public double Pos => 0;

    public bool Homed => true;

    public string State => _state;

    public bool IsMoving { get; private set; }

    public bool IsBlocked => false;

    public string StopReason => "None";

    public string LastError { get; protected set; } = string.Empty;

    public int HomeDir => _homeDir;

    public double Speed { get => _speed; set => _speed = (int)value; }

    public IInputIO? HomeSensor => null;

    public IInputIO? PosLimitSensor => null;

    public IInputIO? NegLimitSensor => null;

    public event Action<double>? PositionChanged;
    public event Action<bool>? HomedChanged;

    public override void ApplyState(JsonElement record)
    {
        if (record.TryGetProperty("state", out var state) && state.ValueKind == JsonValueKind.String)
            _state = state.GetString() ?? "idle";

        if (record.TryGetProperty("running", out var ismoving) && 
            (ismoving.ValueKind == JsonValueKind.False || ismoving.ValueKind == JsonValueKind.True))
            IsMoving = ismoving.GetBoolean();
    }

    public async Task<bool> HomeMotionAsync()
    {
        return true;
    }

    public async Task JogAsync(int dir)
    {
        try
        {
            LastError = string.Empty;

            await _send(Name, "start", new Dictionary<string, object>
            {
                ["target_speed"] = _speed,
                ["direction"] = dir,
                ["friction"] = 1
            }).ConfigureAwait(false);

            await Task.Delay(100);

        }
        catch (Exception ex)
        {
            LastError = ex.Message;
        }
    }

    public async Task<bool> MoveToAsync(double destination, int? timeoutMs = null)
    {
        await Task.CompletedTask;
        return true;
    }

    public bool ServoOn()
    {
        return true;
    }

    public async Task StopAsync()
    {
        try
        {
            await _send(Name, "stop", null).ConfigureAwait(false);
        }
        catch
        {
            // Swallow — disconnect will surface via ConnectionChanged.
        }
    }
}