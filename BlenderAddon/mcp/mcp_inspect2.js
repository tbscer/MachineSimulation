// Inspect only the RotateAxis2 host, very carefully.
const net = require('net');
const HOST = '127.0.0.1';
const PORT = 9876;
let buf = '';
const pending = [];
const sock = net.connect(PORT, HOST, () => { run().catch(e => console.log('FATAL:', e.message)).finally(() => sock.destroy()); });
function tryParse() {
    const t = buf.trim();
    if (!t) return;
    try {
        const m = JSON.parse(t);
        buf = '';
        const w = pending.shift();
        if (!w) return;
        clearTimeout(w.timer);
        if (m.status === 'error' || m.error) w.reject(new Error(JSON.stringify(m)));
        else w.resolve(m);
    } catch(e) {}
}
sock.on('data', d => { buf += d.toString('utf8'); tryParse(); });
sock.on('error', e => console.log('ERR:', e.message));
sock.on('close', () => console.log('closed'));
function send(o) { sock.write(JSON.stringify(o) + '\n'); }
function call(t, p) {
    return new Promise((r, rj) => {
        const w = { resolve: r, reject: rj, timer: setTimeout(() => { const i = pending.indexOf(w); if (i >= 0) pending.splice(i, 1); rj(new Error('timeout')); }, 30000) };
        pending.push(w);
        send({ type: t, params: p });
    });
}

const code = `
import bpy
from MotionSimulation.modules.RotateAxis import discovery, naming
from MotionSimulation.addon import get_manager

host = bpy.data.objects.get('RotateAxis2')
print('host found:', host is not None, 'type:', host.type if host else None)
if host is None:
    print('NO RotateAxis2 host')
else:
    print('children:', [(c.name, c.type, c.parent.name if c.parent else None) for c in host.children])
    cfg = getattr(host, 'rotate_axis', None)
    print('rotate_axis cfg:', cfg is not None)
    if cfg is not None:
        print('  enabled:', bool(cfg.enabled))
        print('  angular_speed:', float(cfg.angular_speed))
        print('  angular_home_speed:', float(cfg.angular_home_speed))
        print('  rotate_axis_index:', int(cfg.rotate_axis_index))
        for f in ('rotate_center','rotator','trigger_shim','rotate_home_sensor','rotate_pos_limit_sensor','rotate_neg_limit_sensor'):
            o = getattr(cfg, f, None)
            if o is None:
                print('  ', f, '=<none>')
            else:
                print('  ', f, '=', o.name, '  parent=', (o.parent.name if o.parent else None))

    mgr = get_manager()
    print('manager:', mgr is not None)
    if mgr is not None:
        ax = mgr.get('2')
        print('runtime:', ax is not None)
        if ax is not None:
            print('  state:', ax.state)
            print('  center.obj:', ax.center.obj.name)
            print('  center.angle:', round(float(ax.center.read_angle()),3))
            print('  rotator.obj:', ax.rotator.obj.name)
            print('  rotator.obj.parent:', ax.rotator.obj.parent.name if ax.rotator.obj.parent else None)
            for p in ('axis_state','axis_current_angle','axis_velocity','axis_stop_reason','axis_cmd_seq','axis_moving'):
                print('  ', p, '=', ax.rotator.obj.get(p, None))
`;

(async () => {
    const r = await call('execute_code', { code });
    console.log(r.result.result);
})();