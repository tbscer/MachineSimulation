using System;
using System.Text.Json;
using Twin.MotionInterface;

namespace Twin.MotionSimulate;

public sealed class SimInputIO : SimObject, IInputIO
{
    private bool _state;

    public bool State => _state;

    public event Action<bool>? StateChanged;

    public SimInputIO(string name) : base(name) { }

    public override void ApplyState(JsonElement record)
    {
        bool newState;
        if (record.TryGetProperty("triggered", out var t))
            newState = t.GetBoolean();
        else if (record.TryGetProperty("state", out var s))
            newState = s.GetBoolean();
        else
            return;

        if (newState == _state) return;
        _state = newState;
        StateChanged?.Invoke(newState);
    }

    /// <summary>
    /// Used by an owning SimAxis when an axis state_push record carries inline
    /// sensor fields (<c>home_sensor</c>/<c>pos_limit</c>/<c>neg_limit</c>).
    /// Forwards to the same StateChanged event as a regular push.
    /// </summary>
    internal void SetFromOwner(bool value)
    {
        if (value == _state) return;
        _state = value;
        StateChanged?.Invoke(value);
    }
}