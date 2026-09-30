using System.Windows;

namespace Twin.Application;

/// <summary>
/// Code-behind is intentionally minimal — all behaviour lives in
/// <see cref="ViewModels.MainWindowViewModel"/>. Only the window-closed
/// lifecycle lives here, to keep disposal deterministic.
/// </summary>
public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        Closed += (_, _) =>
        {
            if (DataContext is ViewModels.MainWindowViewModel vm)
                vm.DisposeRequested();
        };
    }
}