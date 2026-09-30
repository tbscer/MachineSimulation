namespace Twin.MotionSimulate;

/// <summary>
/// Discriminates how an axis payload is encoded on the wire. Linear axes use
/// <c>target_x</c> in move_to payloads; rotary axes use <c>target_angle</c>.
/// </summary>
public enum MotionKind
{
    Linear,
    Rotary,
}