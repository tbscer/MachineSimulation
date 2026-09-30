using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Twin.MotionInterface;

namespace Twin.Application.ViewModels;


public partial class VacuumNozzleViewModel : ObservableObject
{
    private readonly SynchronizationContext _ui;

    [ObservableProperty] private bool _state;

    [ObservableProperty] private bool _isAttached;

    public string Name { get; }
    public IVacuumNozzle Output { get; }

    public VacuumNozzleViewModel(IVacuumNozzle nozzle)
    {
        _ui = SynchronizationContext.Current
              ?? throw new InvalidOperationException(
                  "OutputViewModel must be constructed on the UI thread.");
        Name = nozzle.Name;
        Output = nozzle;

        nozzle.StateChanged += v => _ui.Post(_ => { State = v; IsAttached = nozzle.IsAttached; }, null);
    }

    /// <summary>Called from the View when the user clicks "Set".</summary>
    public void Write(bool value)
    {
        _ = Output.WriteStateAsync(value);
    }

    
    [RelayCommand] private async Task AttachAsync() { try { Write(!State); } catch { } }
    
}