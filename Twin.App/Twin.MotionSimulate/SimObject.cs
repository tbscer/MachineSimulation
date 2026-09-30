using System.Text.Json;

namespace Twin.MotionSimulate;

/// <summary>
/// Base class for every simulation object the runtime knows about. Each subclass
/// implements <see cref="ApplyState"/> to parse its own fields from a state_push
/// record. The runtime only routes records by name; the object knows how to
/// interpret its own data — so adding a new device type is a single new subclass,
/// no runtime changes.
/// </summary>
public abstract class SimObject
{
    public string Name { get; }

    protected SimObject(string name)
    {
        Name = name;
    }

    /// <summary>
    /// Parse the relevant fields from a single state_push record and update cached
    /// state. Called by <see cref="SimMotionCard"/> under its state lock on the RPC
    /// read-loop thread. Implementations should be defensive: missing fields are
    /// silently skipped.
    /// </summary>
    public abstract void ApplyState(JsonElement record);
}