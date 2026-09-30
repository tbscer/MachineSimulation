using System;
using System.Globalization;
using System.Windows.Data;
using System.Windows.Markup;

namespace Twin.Application.Converters;

/// <summary>
/// Converts a bool to one of two strings, chosen by <c>ConverterParameter</c>
/// in the form "trueText|falseText". Defaults to "true|false".
/// </summary>
public sealed class BoolToTextConverter : MarkupExtension, IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        var parts = (parameter as string ?? "true|false").Split('|');
        return (value is bool b && b) ? parts[0] : parts[1];
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotSupportedException();

    private static BoolToTextConverter? _instance;
    public override object ProvideValue(IServiceProvider serviceProvider)
        => _instance ??= new BoolToTextConverter();
}