using System;
using System.Threading;
using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Twin.MotionInterface;

namespace Twin.Application.ViewModels;

/// <summary>
/// MVVM wrapper around <see cref="ICylinder"/>. Exposes the two
/// state sensors as observable properties and offers Extend/Retract commands.
/// </summary>
public partial class CylinderViewModel : ObservableObject
{
    private readonly SynchronizationContext _ui;

    [ObservableProperty] private bool _state1;
    [ObservableProperty] private bool _state2;

    public string Name { get; }
    public ICylinder Cylinder { get; }

    public CylinderViewModel(ICylinder cyl)
    {
        _ui = SynchronizationContext.Current
              ?? throw new InvalidOperationException(
                  "CylinderViewModel must be constructed on the UI thread.");
        Name = cyl.Name;
        Cylinder = cyl;

        cyl.State1.StateChanged += v => _ui.Post(_ => State1 = v, null);
        cyl.State2.StateChanged += v => _ui.Post(_ => State2 = v, null);
    }

    [RelayCommand] private async Task ExtendAsync()  { try { await Cylinder.ActionAsync(true);  } catch { } }
    [RelayCommand] private async Task RetractAsync() { try { await Cylinder.ActionAsync(false); } catch { } }
}