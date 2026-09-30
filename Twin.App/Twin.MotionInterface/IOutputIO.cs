using System;
using System.Threading.Tasks;

namespace Twin.MotionInterface;

public interface IOutputIO
{
    string Name { get; }

    /// Cached state; updated by the subscribe-state push pump.
    bool State { get; }

    /// Raised when <see cref="State"/> changes due to a state push.
    event Action<bool>? StateChanged;

    /// Command the simulation to set this output's state. Returns once the command has
    /// been transmitted; the cached <see cref="State"/> is updated optimistically and
    /// later confirmed by a state push.
    Task WriteStateAsync(bool state);
}

public interface IVacuumNozzle : IOutputIO
{
    /// <summary>
    /// 是否吸附了物体
    /// </summary>
    bool IsAttached { get; }
}