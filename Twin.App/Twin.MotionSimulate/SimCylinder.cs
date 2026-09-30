using System;
using System.Text.Json;
using System.Threading.Tasks;
using Twin.MotionInterface;

namespace Twin.MotionSimulate;

public sealed class SimCylinder : SimObject, ICylinder
{
    private readonly CommandSender _send;

    public IInputIO State1 { get; }
    public IInputIO State2 { get; }
    public IOutputIO Action1 { get; }
    public IOutputIO Action2 { get; }

    internal SimCylinder(string name, CommandSender send,
        SimInputIO state1, SimInputIO state2,
        SimOutputIO action1, SimOutputIO action2)
        : base(name)
    {
        _send = send;
        State1 = state1;
        State2 = state2;
        Action1 = action1;
        Action2 = action2;
    }

    public override void ApplyState(JsonElement record)
    {
        // Tolerant: server may embed state1/state2 inside a cylinder record.
        if (record.TryGetProperty("approach_sensor_1", out var s1))
            ((SimInputIO)State1).SetFromOwner(s1.GetBoolean());
        if (record.TryGetProperty("approach_sensor_2", out var s2))
            ((SimInputIO)State2).SetFromOwner(s2.GetBoolean());
    }

    public async Task ActionAsync(bool stateOne)
    {
        string action = "set_outputs";
        try
        {
            await _send(Name, action, new Dictionary<string, object>
            {
                ["output_1"] = stateOne,
                ["output_2"] = !stateOne,
            }).ConfigureAwait(false);
        }
        catch
        {
            // Swallow — disconnect will surface via ConnectionChanged.
        }
    }
}