using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Twin.Communicate;
using Twin.MotionInterface;

namespace Twin.MotionSimulate;

/// <summary>
/// A digital-twin simulation runtime. Owns the RPC client lifecycle, supervises an
/// auto-reconnect loop with exponential backoff, and dispatches inbound state_push
/// frames into the registered Axis / Cylinder / Input / Output objects.
/// </summary>
public sealed class SimMotionCard : IMotionCard
{
    private BlenderRpcClient? _client;
    private readonly object _stateLock = new();
    private readonly SemaphoreSlim _connectLock = new(1, 1);
    private CancellationTokenSource? _supervisorCts;
    private Task? _supervisorTask;
    private volatile bool _running;

    private volatile bool _connected;
    public bool Connected
    {
        get => _connected;
        private set => _connected = value;
    }

    // ---- collision state (mirrors server's `collision` push record) ----
    private volatile bool _collisionActive;   // server-reported: currently colliding
    private volatile bool _collisionEnabled;  // server-reported: detection on/off

    public bool IsCollide => _collisionActive;
    public bool IsCollisionEnabled => _collisionEnabled;

    public Dictionary<string, IAxis> Axis { get; } = new();
    public Dictionary<string, IInputIO> Inputs { get; } = new();
    public Dictionary<string, IOutputIO> Outputs { get; } = new();
    public Dictionary<string, ICylinder> Cylinders { get; } = new();

    public Dictionary<string, IVacuumNozzle> VacuumNozzles { get; } = new();

    public int StateIntervalMs { get; set; } = 200;
    public bool AutoReconnect { get; set; } = true;

    public event Action? StateUpdated;
    public event Action<bool>? ConnectionChanged;

    // ---- factory registration (call before ConnectAsync) ----

    public SimAxis AddAxis(string name, int homeDir, MotionKind kind = MotionKind.Linear)
    {
        if (Axis.ContainsKey(name))
            throw new ArgumentException($"Axis '{name}' already registered.", nameof(name));
        var axis = new SimAxis(name, kind, SendCommand, homeDir);
        Axis[name] = axis;

        return axis;
    }

    public SimConveyor AddConveyor(string name, int homeDir = -1)
    {
        if (Axis.ContainsKey(name))
            throw new ArgumentException($"Axis '{name}' already registered.", nameof(name));
        var conveyor = new SimConveyor(name, SendCommand, homeDir);
        Axis[name] = conveyor;

        return conveyor;
    }

    public SimInputIO AddInput(string name)
    {
        if (Inputs.ContainsKey(name))
            throw new ArgumentException($"Input '{name}' already registered.", nameof(name));
        var input = new SimInputIO(name);
        Inputs[name] = input;
        return input;
    }

    public SimOutputIO AddOutput(string name)
    {
        if (Outputs.ContainsKey(name))
            throw new ArgumentException($"Output '{name}' already registered.", nameof(name));
        var output = new SimOutputIO(name, SendCommand);
        Outputs[name] = output;
        return output;
    }

    public SimCylinder AddCylinder(string name)
    {
        if (Cylinders.ContainsKey(name))
            throw new ArgumentException($"Cylinder '{name}' already registered.", nameof(name));
        var s1 = new SimInputIO($"{name}_state1");
        var s2 = new SimInputIO($"{name}_state2");
        var a1 = new SimOutputIO($"{name}_action1", SendCommand);
        var a2 = new SimOutputIO($"{name}_action2", SendCommand);
        var cyl = new SimCylinder(name, SendCommand, s1, s2, a1, a2);
        Cylinders[name] = cyl;
        //Inputs[s1.Name] = s1;
        //Inputs[s2.Name] = s2;
        //Outputs[a1.Name] = a1;
        //Outputs[a2.Name] = a2;
        return cyl;
    }

    public SimVacuumNozzle AddVacuumNozzle(string name)
    {
        if (VacuumNozzles.ContainsKey(name))
            throw new ArgumentException($"VacuumNozzle '{name}' already registered.", nameof(name));
        var nozzle = new SimVacuumNozzle(name, SendCommand);
        VacuumNozzles[name] = nozzle;
        return nozzle;
    }



    private void RegisterInputIfNew(IInputIO io)
    {
        if (!Inputs.ContainsKey(io.Name))
            Inputs[io.Name] = io;
    }

    // ---- connection lifecycle ----

    public async Task<bool> ConnectAsync(string host = "127.0.0.1", int port = 9877)
    {
        await _connectLock.WaitAsync().ConfigureAwait(false);
        try
        {
            await StopSupervisorAsync().ConfigureAwait(false);
            DisposeClientSafely();

            _running = true;
            _supervisorCts = new CancellationTokenSource();
            var token = _supervisorCts.Token;
            _supervisorTask = Task.Run(() => SupervisorLoopAsync(host, port, token));

            // Wait up to 5 s for the supervisor to establish the first connection.
            var deadline = Environment.TickCount64 + 5000;
            while (Environment.TickCount64 < deadline)
            {
                if (Connected) return true;
                if (_supervisorTask != null && _supervisorTask.IsFaulted) return false;
                await Task.Delay(50).ConfigureAwait(false);
            }
            return Connected;
        }
        finally
        {
            _connectLock.Release();
        }
    }

    public async Task DisconnectAsync()
    {
        await _connectLock.WaitAsync().ConfigureAwait(false);
        try
        {
            await StopSupervisorAsync().ConfigureAwait(false);
            DisposeClientSafely();
        }
        finally
        {
            _connectLock.Release();
        }
    }

    private async Task StopSupervisorAsync()
    {
        _running = false;
        if (_supervisorCts != null)
        {
            try { _supervisorCts.Cancel(); } catch { }
            _supervisorCts.Dispose();
            _supervisorCts = null;
        }
        if (_supervisorTask != null)
        {
            try { await _supervisorTask.ConfigureAwait(false); }
            catch { /* supervisor may have logged errors; ignore on shutdown */ }
            _supervisorTask = null;
        }
        if (Connected)
        {
            Connected = false;
            ConnectionChanged?.Invoke(false);
        }
    }

    public Task SyncMoveAsync(IAxis[] axes, double[] destines)
    {
        int n = Math.Min(axes.Length, destines.Length);
        var tasks = new Task[n];
        for (int i = 0; i < n; i++)
            tasks[i] = axes[i].MoveToAsync(destines[i]);
        return Task.WhenAll(tasks);
    }

    public void Dispose() => DisconnectAsync().GetAwaiter().GetResult();

    public void UseCollision(bool enable)
    {
        var c = _client;
        if (c == null) return; // not connected — disconnect will surface via ConnectionChanged
        var method = enable ? "enable_collision_detection" : "disable_collision_detection";
        try { _ = c.SendRequestAsync(method); }
        catch { /* disconnect surfaces via ConnectionChanged */ }
    }

    // ---- supervisor with auto-reconnect ----

    private async Task SupervisorLoopAsync(string host, int port, CancellationToken token)
    {
        int backoffMs = 1000;
        const int backoffCapMs = 30_000;

        while (!token.IsCancellationRequested && _running)
        {
            bool ok = await TryConnectOnceAsync(host, port, token).ConfigureAwait(false);
            if (ok)
            {
                backoffMs = 1000;  // reset after successful connect
                await WaitForDisconnectAsync(token).ConfigureAwait(false);
                if (token.IsCancellationRequested) break;
                if (Connected)
                {
                    Connected = false;
                    ConnectionChanged?.Invoke(false);
                }
            }

            if (!AutoReconnect)
                break;

            try { await Task.Delay(backoffMs, token).ConfigureAwait(false); }
            catch (OperationCanceledException) { break; }
            backoffMs = Math.Min(backoffMs * 2, backoffCapMs);
        }
    }

    private async Task<bool> TryConnectOnceAsync(string host, int port, CancellationToken token)
    {
        BlenderRpcClient? c = null;
        bool ownershipTransferred = false;
        try
        {
            c = new BlenderRpcClient();
            c.OnStatePush += OnStatePush;
            c.OnError += msg => System.Diagnostics.Debug.WriteLine($"[Twin] rpc error: {msg}");

            await c.ConnectAsync(host, port).ConfigureAwait(false);

            if (token.IsCancellationRequested) return false;

            _client = c;
            ownershipTransferred = true;
            c = null;  // ownership transferred to _client

            // Subscribe to state pushes. Server may emit pushes regardless of response.
            try
            {
                await _client.SendRequestAsync("subscribe_state", new Dictionary<string, object>
                {
                    ["interval_ms"] = StateIntervalMs,
                }).ConfigureAwait(false);
            }
            catch
            {
                // tolerated — pushes can still arrive without an ack
            }

            if (token.IsCancellationRequested)
            {
                DisposeClientSafely();
                return false;
            }

            Connected = true;
            ConnectionChanged?.Invoke(true);
            return true;
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[Twin] connect failed: {ex.Message}");
            if (ownershipTransferred) DisposeClientSafely();
            if (c != null) { try { c.Dispose(); } catch { } }
            return false;
        }
    }

    private void DisposeClientSafely()
    {
        var c = _client;
        _client = null;
        if (c != null) { try { c.Dispose(); } catch { } }
    }

    private Task WaitForDisconnectAsync(CancellationToken token)
    {
        var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

        var c = _client;
        if (c == null)
        {
            tcs.SetResult();
            return tcs.Task;
        }

        Action handler = () => tcs.TrySetResult();
        c.OnDisconnected += handler;

        token.Register(() =>
        {
            tcs.TrySetResult();
            try { c.OnDisconnected -= handler; } catch { }
        });

        return tcs.Task;
    }

    // ---- state_push dispatch ----

    private void OnStatePush(JsonElement data)
    {
        lock (_stateLock)
        {
            DispatchStatePayload(data);
        }
        StateUpdated?.Invoke();
    }

    private void DispatchStatePayload(JsonElement data)
    {
        if (data.ValueKind == JsonValueKind.Object)
        {
            if (TryArray(data, "axes",      out var axes))  foreach (var r in axes.EnumerateArray())  DispatchAxis(r);
            if (TryArray(data, "cylinders", out var cyls))  foreach (var r in cyls.EnumerateArray())  DispatchCylinder(r);
            if (TryArray(data, "sensors",   out var sens))  foreach (var r in sens.EnumerateArray())  DispatchSensor(r);
            if (TryArray(data, "inputs",    out var inps))  foreach (var r in inps.EnumerateArray())  DispatchSensor(r);
            if (TryArray(data, "outputs",   out var outs))  foreach (var r in outs.EnumerateArray())  DispatchOutput(r);
            if (TryArray(data, "vacuum_nozzles", out var nozzles)) foreach (var r in nozzles.EnumerateArray()) DispatchVacuumNozzle(r);
            if (TryArray(data, "records",   out var recs))  foreach (var r in recs.EnumerateArray())  DispatchFlat(r);
            if (data.TryGetProperty("collision", out var coll)) DispatchCollision(coll);
        }
        else if (data.ValueKind == JsonValueKind.Array)
        {
            foreach (var r in data.EnumerateArray()) DispatchFlat(r);
        }
    }

    private static bool TryArray(JsonElement obj, string name, out JsonElement value)
    {
        value = default;
        if (!obj.TryGetProperty(name, out value)) return false;
        return value.ValueKind == JsonValueKind.Array;
    }

    private void DispatchAxis(JsonElement r)
    {
        if (!TryGetName(r, out var name)) return;
        if (!Axis.TryGetValue(name, out var axis)) return;
        if (axis is SimAxis simAxis)
            simAxis.ApplyState(r);
        else if (axis is SimConveyor conveyor)
            conveyor.ApplyState(r);
    }

    private void DispatchCylinder(JsonElement r)
    {
        if (!TryGetName(r, out var name)) return;
        if (Cylinders.TryGetValue(name, out var cyl) && cyl is SimCylinder sc)
            sc.ApplyState(r);
    }

    private void DispatchSensor(JsonElement r)
    {
        if (!TryGetName(r, out var name)) return;
        if (Inputs.TryGetValue(name, out var inp) && inp is SimInputIO si)
            si.ApplyState(r);
    }

    private void DispatchOutput(JsonElement r)
    {
        if (!TryGetName(r, out var name)) return;
        if (Outputs.TryGetValue(name, out var outp) && outp is SimOutputIO so)
            so.ApplyState(r);
    }

    private void DispatchVacuumNozzle(JsonElement r)
    {
        if (!TryGetName(r, out var name)) return;
        if (VacuumNozzles.TryGetValue(name, out var nozzle) && nozzle is SimVacuumNozzle sn)
            sn.ApplyState(r);
    }

    /// <summary>Tolerant: dispatch a record by name lookup across all four dictionaries.</summary>
    private void DispatchFlat(JsonElement r)
    {
        if (!TryGetName(r, out var name)) return;
        if (Axis.TryGetValue(name, out var axis))
        {
            if (axis is SimAxis simAxis) simAxis.ApplyState(r);
            else if (axis is SimConveyor conveyor) conveyor.ApplyState(r);
            return;
        }
        if (Cylinders.TryGetValue(name, out var cyl) && cyl is SimCylinder sc) { sc.ApplyState(r); return; }
        if (Inputs.TryGetValue(name, out var inp) && inp is SimInputIO si) { si.ApplyState(r); return; }
        if (Outputs.TryGetValue(name, out var outp) && outp is SimOutputIO so) { so.ApplyState(r); return; }
    }

    private static bool TryGetName(JsonElement r, out string name)
    {
        name = string.Empty;
        if (r.ValueKind != JsonValueKind.Object) return false;
        if (!r.TryGetProperty("name", out var n) || n.ValueKind != JsonValueKind.String) return false;
        var s = n.GetString();
        if (string.IsNullOrEmpty(s)) return false;
        name = s;
        return true;
    }

    private static bool TryReadBool(JsonElement parent, string name, out bool value)
    {
        value = false;
        if (!parent.TryGetProperty(name, out var el)) return false;
        if (el.ValueKind != JsonValueKind.True && el.ValueKind != JsonValueKind.False) return false;
        value = el.GetBoolean();
        return true;
    }

    private void DispatchCollision(JsonElement r)
    {
        if (r.ValueKind != JsonValueKind.Object) return;
        if (TryReadBool(r, "enabled", out var e)) _collisionEnabled = e;
        if (TryReadBool(r, "active",  out var a)) _collisionActive  = a;
    }

    // ---- command sender closure handed to every Sim* object ----

    private Task<JsonElement> SendCommand(string moduleId, string action, object? payload)
    {
        var c = _client ?? throw new InvalidOperationException("Not connected.");
        return c.SendRequestAsync("apply_command", new Dictionary<string, object>
        {
            ["module_id"] = moduleId,
            ["action"] = action,
            ["payload"] = payload ?? new Dictionary<string, object>(),
        });
    }
}