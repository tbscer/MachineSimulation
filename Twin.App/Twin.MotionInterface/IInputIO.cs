using System;

namespace Twin.MotionInterface;

public interface IInputIO
{
    string Name { get; }

    /// Cached state; updated by the subscribe-state push pump.
    bool State { get; }

    /// Raised when <see cref="State"/> changes due to a state push.
    event Action<bool>? StateChanged;
}