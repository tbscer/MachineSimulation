using System;
using System.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using Twin.MotionInterface;

namespace Twin.Application.ViewModels;

public partial class OutputViewModel : ObservableObject
{
    private readonly SynchronizationContext _ui;

    [ObservableProperty] private bool _state;

    public string Name { get; }
    public IOutputIO Output { get; }

    public OutputViewModel(IOutputIO output)
    {
        _ui = SynchronizationContext.Current
              ?? throw new InvalidOperationException(
                  "OutputViewModel must be constructed on the UI thread.");
        Name = output.Name;
        Output = output;

        output.StateChanged += v => _ui.Post(_ => State = v, null);
    }

    /// <summary>Called from the View when the user clicks "Set".</summary>
    public void Write(bool value)
    {
        _ = Output.WriteStateAsync(value);
    }
}