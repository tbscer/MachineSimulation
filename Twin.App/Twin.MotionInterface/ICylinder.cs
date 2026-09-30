using System.Threading.Tasks;

namespace Twin.MotionInterface;

public interface ICylinder
{
    string Name { get; }

    IInputIO State1 { get; }
    IInputIO State2 { get; }

    IOutputIO Action1 { get; }
    IOutputIO Action2 { get; }

    /// Drive the cylinder: true → extend (Action1), false → retract (Action2).
    Task ActionAsync(bool stateOne);
}