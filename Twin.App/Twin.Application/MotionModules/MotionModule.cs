using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Twin.MotionInterface;

namespace Twin.Application.MotionModules;

internal abstract class MotionModule
{
    public Dictionary<string, IAxis> Axes { get; } = new();

    public abstract void Init(IMotionCard card);

    public abstract Task Home();
}


internal class BondHead : MotionModule
{

    private bool isLeft;

    public IAxis X { get; protected set; }
    public IAxis Y { get; protected set; }
    public IAxis Z { get; protected set; }

    public IAxis R { get; protected set; }

    public BondHead(bool isLeft)
    {
        this.isLeft = isLeft;
    }



    public override void Init(IMotionCard card)
    {
        // Implementation for initializing BondHead

        X = card.Axis[isLeft ? "LinearAxisX" : "LinearAxisX2"];
        Y = card.Axis[isLeft ? "LinearAxisY" : "LinearAxisY2"];
        Z = card.Axis[isLeft ? "LinearAxisZ" : "LinearAxisZ2"];
        R = card.Axis[isLeft ? "RotateAxis.R" : "RotateAxis.R2"];
    }

    public override async Task Home()
    {
        Z.Speed = 50;
        await Z.HomeMotionAsync().ConfigureAwait(false);

        X.Speed = Y.Speed = 80;
        R.Speed = 50;
        Task xt = X.HomeMotionAsync();
        Task yt = Y.HomeMotionAsync();
        Task rt = R.HomeMotionAsync();

        await Task.WhenAll(xt, yt, rt).ConfigureAwait(false);

        Y.Speed = 100; // Reset Y speed to default after homing
        if (isLeft)
        {
            await Y.MoveToAsync(200);
        }
        else
        {
            await Y.MoveToAsync(-200);
        }
    }

    public async Task<bool> HomeZ()
    {
        Z.Speed = 50;
        return await Z.HomeMotionAsync().ConfigureAwait(false);
    }

    public async Task<bool> HomeY()
    {
        Y.Speed = 80;

        bool yHomed = await Y.HomeMotionAsync().ConfigureAwait(false);
        if (!yHomed)
        {
            return false;
        }

        Y.Speed = 100; // Reset Y speed to default after homing
        if (isLeft)
        {
            await Y.MoveToAsync(200);
        }
        else
        {
            await Y.MoveToAsync(-200);
        }

        return true;
    }

    public async Task<bool> HomeXR()
    {
        X.Speed = 80;
        R.Speed = 50;

        

        Task<bool> xt = X.HomeMotionAsync();
        Task<bool> rt = R.HomeMotionAsync();
        await Task.WhenAll(xt, rt).ConfigureAwait(false);

        return xt.Result && rt.Result;
    }

    /// <summary>
    /// Jog Y until the mechanical limit is hit, then stop the axis.
    /// Left bond head jogs toward the positive limit; right bond head jogs
    /// toward the negative limit.
    /// Returns true if the target limit sensor tripped within the timeout.
    /// </summary>
    public async Task<bool> JogLimitY(int timeoutMs = 30000)
    {
        Y.Speed = 100;

        int dir = isLeft ? +1 : -1;
        IInputIO? targetLimit = isLeft ? Y.PosLimitSensor : Y.NegLimitSensor;
        IInputIO? oppositeLimit = isLeft ? Y.NegLimitSensor : Y.PosLimitSensor;

        // Already parked on the target limit: nothing to do.
        if (targetLimit?.State == true)
        {
            await Y.StopAsync().ConfigureAwait(false);
            return true;
        }

        await Y.JogAsync(dir).ConfigureAwait(false);

        // Stop as soon as the target limit trips, or bail out if the axis
        // somehow runs into the opposite limit (e.g. wrong jog direction).
        await WaitForAsync(
            () => targetLimit?.State == true || oppositeLimit?.State == true,
            timeoutMs).ConfigureAwait(false);

        await Y.StopAsync().ConfigureAwait(false);

        return targetLimit?.State == true;
    }

    private static async Task<bool> WaitForAsync(Func<bool> condition, int timeoutMs)
    {
        long deadline = Environment.TickCount64 + timeoutMs;
        while (!condition())
        {
            if (Environment.TickCount64 >= deadline) return false;
            await Task.Delay(50).ConfigureAwait(false);
        }
        return true;
    }

    public void SetWorkSpeed()
    {
        X.Speed = 80;
        Y.Speed = 120;
        Z.Speed = 100;
        R.Speed = 50;
    }
}

internal class Flipper : MotionModule
{
    private bool isLeft;

    public IAxis Z { get; protected set; }

    public IAxis R { get; protected set; }

    public IVacuumNozzle Nozzle { get; protected set; }

    public Flipper(bool isLeft)
    {
        this.isLeft = isLeft;
    }

    public override void Init(IMotionCard card)
    {
        // Implementation for initializing BondHead

        Z = card.Axis[isLeft ? "LinearAxis.Rotate.LZ" : "LinearAxis.Rotate.RZ"];
        R = card.Axis[isLeft ? "RotateAxis.LZ" : "RotateAxis.RZ"];

        Nozzle = card.VacuumNozzles[isLeft ? "VacuumNozzle.LZ" : "VacuumNozzle.RZ"];
    }

    public override async Task Home()
    {
        Z.Speed = 50;
        await Z.HomeMotionAsync().ConfigureAwait(false);

        R.Speed = 50;

        await R.HomeMotionAsync().ConfigureAwait(false);
    }

    public void SetWorkSpeed()
    {
        Z.Speed = 80;
        R.Speed = 120;
    }
}

internal class WaferTable : MotionModule
{
    private const int NegLimitTimeoutMs = 30000;

    public IAxis X { get; protected set; }

    public IAxis Y { get; protected set; }

    public IAxis NZ { get; protected set; }

    public WaferTable()
    {
    }

    public override void Init(IMotionCard card)
    {
        // Implementation for initializing WaferTable

        X = card.Axis["LinearAxis.WX"];
        Y = card.Axis["LinearAxis.WY"];
        NZ = card.Axis["LinearAxis.NZ"];
    }

    public override async Task Home()
    {
        X.Speed = 80;
        Y.Speed = 80;
        NZ.Speed = 50;

        await JogNZToNegLimitAsync(CancellationToken.None).ConfigureAwait(false);

        Task<bool> xt = X.HomeMotionAsync();
        Task<bool> rt = Y.HomeMotionAsync();
        await Task.WhenAll(xt, rt).ConfigureAwait(false);

        await NZ.HomeMotionAsync().ConfigureAwait(false);
    }

    public async Task<bool> JogNZToNegLimitAsync(CancellationToken ct)
    {
        IInputIO? negLimit = NZ.NegLimitSensor;
        if (negLimit is null) return false;

        if (negLimit.State)
        {
            await NZ.StopAsync().ConfigureAwait(false);
            return true;
        }

        NZ.Speed = 50;
        await NZ.JogAsync(-1).ConfigureAwait(false);

        long deadline = Environment.TickCount64 + NegLimitTimeoutMs;
        bool moved = false;
        try
        {
            while (Environment.TickCount64 < deadline)
            {
                ct.ThrowIfCancellationRequested();

                if (negLimit.State) return true;
                if (NZ.PosLimitSensor?.State == true || NZ.IsBlocked) return false;
                if (NZ.IsMoving) moved = true;
                else if (moved) return false;

                await Task.Delay(50, ct).ConfigureAwait(false);
            }

            return false;
        }
        finally
        {
            await NZ.StopAsync().ConfigureAwait(false);
        }
    }
}