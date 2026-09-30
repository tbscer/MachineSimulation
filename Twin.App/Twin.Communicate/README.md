# Twin.Communicate

Digital Twin 系统的 gRPC 通讯层，作为客户端与 Blender 3D 系统通讯。

## 架构

通讯分为三个服务区域，每个区域的数据通过 JSON 字符串承载，动态可扩展：

| 服务 | 用途 |
|------|------|
| AxesService | 运动轴控制（直线/旋转） |
| CylindersService | 气缸/液压缸控制 |
| SensorsService | 传感器反馈获取 |

## 通讯模式

| 方法 | 模式 | 用途 |
|------|------|------|
| `SendCommand` | 一元调用 | 发送控制指令，返回 Ack 确认 |
| `GetState` | 服务端流 | 一次性获取所有对象状态 |
| `Subscribe` | 服务端流 | 持续订阅状态变化 |

## C# 客户端使用

```csharp
using Twin.Communicate;
using Twin.Communicate.Grpc;

var client = new TwinGrpcClient("http://localhost:50051");

// === Axes：控制运动轴 ===
var ack = await client.Axes.SendCommandAsync(new JsonPayload
{
    Id = "Axis_X",
    Json = """{"name": "Axis_X", "location": [100.0, 0.0, 0.0]}"""
});

// 获取所有轴状态（服务端流）
using var axesStream = client.Axes.GetState(new Empty());
await foreach (var state in axesStream.ResponseStream.ReadAllAsync())
{
    Console.WriteLine($"{state.Id}: {state.Json}");
}

// === Cylinders：控制气缸 ===
await client.Cylinders.SendCommandAsync(new JsonPayload
{
    Id = "Cylinder_1",
    Json = """{"name": "Cylinder_1", "scale": [1.0, 1.0, 2.5]}"""
});

// === Sensors：读取传感器 ===
using var sensorStream = client.Sensors.GetState(new Empty());
await foreach (var sensor in sensorStream.ResponseStream.ReadAllAsync())
{
    Console.WriteLine($"{sensor.Id}: {sensor.Json}");
}
```

## JSON 载荷约定

`JsonPayload.Json` 字段为自由格式 JSON，按需扩展，不需要修改 proto 文件：

```json
// Axis 命令
{"name": "Axis_X", "location": [10, 0, 0], "rotation_euler": [0, 0, 1.57]}

// Cylinder 命令
{"name": "Cylinder_1", "extend": true}

// Sensor 反馈（服务端返回）
{"name": "HomeSensor_X", "triggered": true, "location": [0, 0, 0]}
```

## Blender 端安装

### 前置条件

Blender 需要安装 `grpcio` 和 `grpcio-tools` Python 包。

1. 找到 Blender 自带的 Python 路径（Edit > Preferences > System > Python），或使用系统 Python：

```bash
pip install grpcio grpcio-tools
```

2. 如果使用 Blender 自带 Python（Windows 示例）：

```bash
"C:\Program Files\Blender Foundation\Blender 4.x\4.x\python\bin\python.exe" -m pip install grpcio grpcio-tools
```

### 安装插件

1. 打开 Blender，进入 Edit > Preferences > Add-ons
2. 点击 Install...，选择项目中的 `BlenderServer` 文件夹（作为文件夹安装）
   - 或者将 `BlenderServer` 文件夹复制到 Blender 插件目录：
     - Windows: `%APPDATA%\Blender Foundation\Blender\4.x\scripts\addons\`
     - Linux: `~/.config/blender/4.x/scripts/addons/`
     - macOS: `~/Library/Application Support/Blender/4.x/scripts/addons/`
3. 在插件列表中搜索 "Twin gRPC Server"，勾选启用

### 启用后

- gRPC 服务器自动在 `50051` 端口启动
- 禁用插件时服务器自动停止

### 标记 Blender 对象

在 Blender 中通过自定义属性 `twin_type` 标记对象归属哪个服务：

```python
import bpy

# 标记运动轴
bpy.data.objects["Axis_X"]["twin_type"] = "axis"

# 标记气缸
bpy.data.objects["Cylinder_1"]["twin_type"] = "cylinder"

# 标记传感器
bpy.data.objects["HomeSensor_X"]["twin_type"] = "sensor"
```

也可以在 Object Properties > Custom Properties 面板中手动添加 `twin_type` 属性。

## 项目结构

```
Twin.Communicate/
├── Protos/
│   └── twin.proto          # gRPC 服务定义
├── TwinGrpcClient.cs       # 客户端包装类
└── Twin.Communicate.csproj

BlenderServer/
├── __init__.py             # Blender 插件入口
├── server.py               # gRPC 服务器实现
├── twin.proto              # proto 定义（副本）
├── twin_pb2.py             # 生成的消息代码
└── twin_pb2_grpc.py        # 生成的服务代码
```