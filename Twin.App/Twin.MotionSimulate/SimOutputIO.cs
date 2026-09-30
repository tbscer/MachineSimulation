using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading.Tasks;
using Twin.MotionInterface;

namespace Twin.MotionSimulate;

public class SimOutputIO : SimObject, IOutputIO
{
    internal readonly CommandSender _send;
    protected bool _state;

    public bool State
    {
        get => _state;
        protected set
        {
            if (value != _state)
            {
                _state = value;
                StateChanged?.Invoke(value);
            }
        }
    }

    public event Action<bool>? StateChanged;

    internal SimOutputIO(string name, CommandSender send) : base(name)
    {
        _send = send;
    }

    public override void ApplyState(JsonElement record)
    {
        if (!record.TryGetProperty("state", out var s)) return;
        bool newState = s.GetBoolean();
        if (newState == _state) return;
        State = newState;
    }

    public virtual async Task WriteStateAsync(bool state)
    {
        try
        {
            await _send(Name, "write_state", new Dictionary<string, object>
            {
                ["state"] = state,
            }).ConfigureAwait(false);
        }
        catch
        {
            // Disconnected / send failed — leave cached state untouched; the next
            // state_push will reconcile.
        }

        // Optimistic local update — server push will confirm.
        State = state;
    }

}

public class SimVacuumNozzle : SimOutputIO, IVacuumNozzle
{
    public bool IsAttached { get; protected set; }

    internal SimVacuumNozzle(string name, CommandSender send) : base(name, send)
    {
    }

    public override void ApplyState(JsonElement record)
    {
        if (!record.TryGetProperty("enabled", out var s)) return;
        bool newState = s.GetBoolean();
        
        State = newState;

        if (record.TryGetProperty("held_count", out var a))
        {
            int newAttached = a.GetInt32();

            IsAttached = newAttached > 0;
        }
    }

    public override async Task WriteStateAsync(bool state)
    {
        try
        {
            await _send(Name, "set_enabled", new Dictionary<string, object>
            {
                ["enabled"] = state,
            }).ConfigureAwait(false);
        }
        catch
        {
            // Disconnected / send failed — leave cached state untouched; the next
            // state_push will reconcile.
        }

        // Optimistic local update — server push will confirm.
        State = state;
    }
}