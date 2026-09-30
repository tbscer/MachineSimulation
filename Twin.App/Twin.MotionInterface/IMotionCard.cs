using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace Twin.MotionInterface;

/// <summary>
/// A digital-twin simulation runtime that talks to Blender over the project's
/// TCP JSON-RPC protocol. Owns the connection lifecycle, supervises auto-reconnect,
/// and dispatches inbound state_push frames into the registered Axis / Cylinder /
/// Input / Output objects.
/// </summary>
public interface IMotionCard : IDisposable
{
    /// <summary>
    /// 是否发生碰撞（来自 state_push.collision.active）。
    /// </summary>
    bool IsCollide { get; }

    /// <summary>
    /// 服务器报告的碰撞检测开关状态（来自 state_push.collision.enabled）。
    /// 与 <see cref="UseCollision"/> 设置的本地意图可能短暂不一致，直到服务器回 push 确认。
    /// </summary>
    bool IsCollisionEnabled { get; }

    bool Connected { get; }

    Dictionary<string, IAxis> Axis { get; }
    Dictionary<string, IInputIO> Inputs { get; }
    Dictionary<string, IOutputIO> Outputs { get; }
    Dictionary<string, ICylinder> Cylinders { get; }

    Dictionary<string, IVacuumNozzle> VacuumNozzles { get; }


    /// Open the RPC connection and start the subscribe-state push pump.
    /// Returns true on success, false if the connection could not be established.
    Task<bool> ConnectAsync(string host = "127.0.0.1", int port = 9877);

    /// Cancel the supervisor, dispose the RPC client, and tear down background loops.
    Task DisconnectAsync();

    /// Move a batch of axes to their respective destinations in parallel.
    Task SyncMoveAsync(IAxis[] axes, double[] destines);

    /// <summary>
    /// Enable or disable collision detection in the simulation. When enabled, the simulation will check for collisions between objects and respond accordingly. 
    /// When disabled, objects can pass through each other without any collision response.
    /// </summary>
    /// <param name="enable"></param>
    void UseCollision(bool enable); 

    /// Raised after each state_push has been dispatched into all registered objects.
    /// Fires on the RPC read-loop thread — UI handlers MUST marshal via Dispatcher.
    event Action? StateUpdated;

    /// Raised when Connected transitions. Argument is the new value (true / false).
    event Action<bool>? ConnectionChanged;
}