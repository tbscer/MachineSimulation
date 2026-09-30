using System;
using System.Threading;
using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Twin.MotionInterface;

namespace Twin.Application.ViewModels;

/// <summary>
/// MVVM wrapper around <see cref="IAxis"/>. Forwards model events
/// (<c>PositionChanged</c>, <c>HomedChanged</c>, sensor <c>StateChanged</c>)
/// into observable properties so XAML bindings can drive the UI.
///
/// All setters are invoked on the UI thread via the captured
/// <see cref="SynchronizationContext"/>; model events fire on the RPC
/// read-loop thread.
/// </summary>
public partial class AxisViewModel : ObservableObject
{
    private readonly SynchronizationContext _ui;

    [ObservableProperty] private double _position;
    [ObservableProperty] private bool _homed;
    [ObservableProperty] private bool _homeSensor;
    [ObservableProperty] private bool _posLimit;
    [ObservableProperty] private bool _negLimit;

    /// <summary>User-editable speed. Forwarded to <see cref="IAxis.Speed"/>.</summary>
    [ObservableProperty] private double _speed = 100.0;

    /// <summary>User-editable target position for the Move button.</summary>
    [ObservableProperty] private string _targetText = string.Empty;

    public string Name { get; }
    public IAxis Axis { get; }

    public AxisViewModel(IAxis axis)
    {
        _ui = SynchronizationContext.Current
              ?? throw new InvalidOperationException(
                  "AxisViewModel must be constructed on the UI thread so SynchronizationContext is the WPF dispatcher context.");
        Name = axis.Name;
        Axis = axis;
        _speed = axis.Speed;

        axis.PositionChanged += p => _ui.Post(_ => Position = p, null);
        axis.HomedChanged    += h => _ui.Post(_ => Homed = h,    null);
        if (axis.HomeSensor is { } homeSensor)
            homeSensor.StateChanged += s => _ui.Post(_ => HomeSensor = s, null);
        if (axis.PosLimitSensor is { } posLimitSensor)
            posLimitSensor.StateChanged += s => _ui.Post(_ => PosLimit = s, null);
        if (axis.NegLimitSensor is { } negLimitSensor)
            negLimitSensor.StateChanged += s => _ui.Post(_ => NegLimit = s, null);
    }

    partial void OnSpeedChanged(double value) => Axis.Speed = value;

    [RelayCommand]
    private async Task MoveToAsync()
    {
        if (!double.TryParse(TargetText, out var dest)) return;
        try { await Axis.MoveToAsync(dest); } catch { /* surface via log if needed */ }
    }

    [RelayCommand] private async Task HomeAsync()     { try { await Axis.HomeMotionAsync(); } catch { } }
    [RelayCommand] private async Task JogNegativeAsync() { try { await Axis.JogAsync(-1);    } catch { } }
    [RelayCommand] private async Task JogPositiveAsync() { try { await Axis.JogAsync(+1);    } catch { } }
    [RelayCommand] private async Task StopAsync()      { try { await Axis.StopAsync();      } catch { } }
}