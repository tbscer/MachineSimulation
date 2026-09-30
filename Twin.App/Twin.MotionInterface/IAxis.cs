using System;
using System.Threading.Tasks;

namespace Twin.MotionInterface;

public interface IAxis
{
    string Name { get; }

    /// Cached current position; updated by the subscribe-state push pump.
    double Pos { get; }

    /// Cached homed flag; updated by the subscribe-state push pump.
    bool Homed { get; }

    /// Last server-reported motion state (``idle`` / ``moving_p2p`` / ``moving_vel`` /
    /// ``homing`` / ``stopped_at_limit`` / ``blocked``). Empty until the first push.
    string State { get; }

    /// True while the axis reports an in-progress motion. Completion detection must use
    /// this instead of "state == idle": a blocked or limit-stopped axis has finished
    /// moving even though it is not idle.
    bool IsMoving { get; }

    /// True when the server has latched a collision block on this axis. New motion
    /// commands are refused until the block is cleared (disable + re-enable collision
    /// detection from the app, or CollisionEngine.clear_collision() in Blender).
    bool IsBlocked { get; }

    /// Server-reported reason the last motion ended (e.g. ``arrived`` / ``collision``);
    /// empty while moving. Useful for diagnostics.
    string StopReason { get; }

    /// Last command error reported by the server for this axis (e.g. ``AXIS_BLOCKED``);
    /// cleared when a new command is accepted.
    string LastError { get; }

    /// The direction in which the axis should be moved to home.
    int HomeDir { get; }

    /// Speed (units/sec) used to derive motion duration. Settable from UI.
    double Speed { get; set; }

    IInputIO? HomeSensor { get; }
    IInputIO? PosLimitSensor { get; }
    IInputIO? NegLimitSensor { get; }

    /// Raised when <see cref="Pos"/> changes due to a state push. Argument is the new value.
    event Action<double>? PositionChanged;

    /// Raised when <see cref="Homed"/> changes due to a state push. Argument is the new value.
    event Action<bool>? HomedChanged;

    /// Lightweight capability probe. Returns true if the axis responds (e.g. servo on).
    bool ServoOn();

    /// Initiate a homing sequence. Returns true if the homed condition is observed
    /// within the implementation's timeout, false on timeout.
    Task<bool> HomeMotionAsync();

    /// Move to the given destination. Awaitable; implementation typically polls cached
    /// state until the destination is reached or the timeout elapses.
    /// <paramref name="timeoutMs"/> overrides the implementation's default wait budget;
    /// a timeout (or a blocked / limit-stopped motion) returns false instead of hanging.
    Task<bool> MoveToAsync(double destination, int? timeoutMs = null);

    /// Start jogging in the given direction (-1 / +1). Returns once the command is sent;
    /// does not wait for the motion to settle.
    Task JogAsync(int dir);

    /// Stop any in-progress motion. Returns once the command is sent.
    Task StopAsync();
}