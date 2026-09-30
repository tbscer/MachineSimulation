using System.Text.Json;
using System.Threading.Tasks;

namespace Twin.MotionSimulate;

/// <summary>
/// Internal delegate used by SimObject subclasses to send apply_command messages
/// to Blender without holding a direct reference to the RPC client.
/// </summary>
public delegate Task<JsonElement> CommandSender(string moduleId, string action, object? payload);