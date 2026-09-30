using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Twin.Communicate;


public class BlenderRpcClient : IDisposable
{
    private TcpClient? _client;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private CancellationTokenSource? _cts;
    private int _nextId = 0;
    private volatile bool _disposed;

    // Serializes concurrent SendRequestAsync calls (UI command + keep-alive + subscribe).
    // StreamWriter is not thread-safe; without this lock interleaved writes can corrupt
    // the newline-delimited JSON stream.
    private readonly SemaphoreSlim _writeLock = new(1, 1);

    // Maps request IDs to awaiting tasks
    private readonly ConcurrentDictionary<int, TaskCompletionSource<JsonElement>> _pendingRequests = new();

    public event Action<JsonElement>? OnStatePush;
    public event Action<string>? OnError;
    public event Action? OnDisconnected;

    public async Task ConnectAsync(string host = "127.0.0.1", int port = 9877)
    {
        // Re-allow sends after a previous Disconnect. Caller is responsible for
        // calling Disconnect before re-connecting so background loops are torn down.
        _disposed = false;
        _cts?.Dispose();
        _cts = new CancellationTokenSource();

        _client = new TcpClient();
        await _client.ConnectAsync(host, port);

        var stream = _client.GetStream();
        _reader = new StreamReader(stream, System.Text.Encoding.UTF8);
        _writer = new StreamWriter(stream, new System.Text.UTF8Encoding(false)) { AutoFlush = true };

        _ = ReadLoopAsync();
        _ = KeepAliveLoopAsync();
    }

    public async Task<JsonElement> SendRequestAsync(string method, object? parameters = null)
    {
        if (_disposed) throw new ObjectDisposedException(nameof(BlenderRpcClient));

        int id = Interlocked.Increment(ref _nextId);

        // Construct the payload dictionary
        var reqDict = new Dictionary<string, object>
        {
            ["id"] = id,
            ["method"] = method,
            ["params"] = parameters ?? new Dictionary<string, object>()
        };

        string json = JsonSerializer.Serialize(reqDict);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pendingRequests[id] = tcs;

        // Serialize concurrent writers (UI command + keep-alive ping + subscribe_state)
        await _writeLock.WaitAsync().ConfigureAwait(false);
        try
        {
            var writer = _writer ?? throw new InvalidOperationException("Not connected.");
            // Write newline-delimited JSON message
            await writer.WriteAsync(json + "\n").ConfigureAwait(false);
        }
        finally
        {
            _writeLock.Release();
        }

        return await tcs.Task.ConfigureAwait(false);
    }

    private async Task ReadLoopAsync()
    {
        try
        {
            while (_cts != null && !_cts.Token.IsCancellationRequested)
            {
                string? line = await _reader!.ReadLineAsync();
                if (line == null)
                {
                    await Task.Delay(100); // Wait a bit before checking again
                    break; // EOF
                }
                

                using var doc = JsonDocument.Parse(line);
                var root = doc.RootElement;

                // Handle Push Messages
                if (root.TryGetProperty("type", out var typeProp) && typeProp.GetString() == "state_push")
                {
                    if (root.TryGetProperty("data", out var dataProp))
                    {
                        OnStatePush?.Invoke(dataProp.Clone());
                    }
                }
                // Handle Responses to Requests
                else if (root.TryGetProperty("id", out var idProp) && idProp.ValueKind == JsonValueKind.Number)
                {
                    int id = idProp.GetInt32();
                    if (_pendingRequests.TryRemove(id, out var tcs))
                    {
                        if (root.TryGetProperty("ok", out var okProp) && okProp.GetBoolean())
                        {
                            root.TryGetProperty("result", out var res);
                            tcs.SetResult(res.Clone());
                        }
                        else
                        {
                            root.TryGetProperty("error", out var err);
                            tcs.SetException(new Exception(err.ToString()));
                        }
                    }
                }
            }
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"Connection error: {ex.Message}");
        }
        finally
        {
            OnDisconnected?.Invoke();
        }
    }

    private async Task KeepAliveLoopAsync()
    {
        try
        {
            // 服务端超时时间为 5.0 秒，因此我们每 3 秒发送一次 ping 以保持连接活跃
            while (_cts != null && !_cts.Token.IsCancellationRequested)
            {
                await Task.Delay(3000, _cts.Token);

                if (_cts.Token.IsCancellationRequested)
                    break;

                try
                {
                    // 发送静默的心跳包
                    await SendRequestAsync("ping");
                }
                catch
                {
                    // 忽略 ping 的异常（例如正好在断开连接时发生）
                    // 真正的断开逻辑交由 ReadLoopAsync 处理
                }
            }
        }
        catch (TaskCanceledException)
        {
            // 正常取消，退出循环
        }
    }

    public void Disconnect()
    {
        if (_disposed) return;
        _disposed = true;

        _cts?.Cancel();

        // Take the write lock so any in-flight SendRequestAsync finishes before we
        // tear down the underlying streams. After release, future calls will see
        // _writer == null and throw InvalidOperationException.
        _writeLock.Wait();
        try
        {
            _reader?.Dispose();
            _writer?.Dispose();
            _client?.Dispose();
            _reader = null;
            _writer = null;
        }
        finally
        {
            _writeLock.Release();
        }

        // Fail any pending requests
        foreach (var kvp in _pendingRequests)
        {
            kvp.Value.TrySetException(new Exception("Disconnected."));
        }
        _pendingRequests.Clear();
    }

    public void Dispose()
    {
        Disconnect();
    }
}