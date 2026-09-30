using System;
using System.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using Twin.MotionInterface;

namespace Twin.Application.ViewModels;

public partial class InputViewModel : ObservableObject
{
    private readonly SynchronizationContext _ui;

    [ObservableProperty] private bool _state;

    public string Name { get; }
    public IInputIO Input { get; }

    public InputViewModel(IInputIO input)
    {
        _ui = SynchronizationContext.Current
              ?? throw new InvalidOperationException(
                  "InputViewModel must be constructed on the UI thread.");
        Name = input.Name;
        Input = input;

        input.StateChanged += v => _ui.Post(_ => State = v, null);
    }
}