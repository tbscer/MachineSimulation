// Minimal debug: send get_scene_info and dump everything that comes back.
const net = require('net');
const sock = net.connect(9876, '127.0.0.1', () => {
    console.log('connected');
    sock.write(JSON.stringify({type: 'get_scene_info', params: {}}) + '\n');
});
let buf = '';
sock.on('data', d => {
    const chunk = d.toString('utf8');
    console.log('CHUNK len=', chunk.length, 'has-newline=', chunk.includes('\n'));
    console.log('FIRST 200:', JSON.stringify(chunk.slice(0, 200)));
    console.log('LAST 100:', JSON.stringify(chunk.slice(-100)));
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        console.log('LINE:', line.slice(0, 300));
    }
    console.log('REMAINING BUF len=', buf.length);
    if (buf.length > 0) {
        try { JSON.parse(buf); console.log('  WHOLE BUF valid JSON, len=', buf.length); }
        catch (e) { console.log('  parse fail:', e.message); }
    }
});
sock.on('error', e => console.log('ERR:', e.message));
sock.on('close', () => console.log('closed'));
sock.setTimeout(8000, () => { console.log('TIMEOUT'); sock.destroy(); });