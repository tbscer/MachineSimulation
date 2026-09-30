const net = require('net');
const HOST='127.0.0.1', PORT=9876;
const sock = net.connect(PORT, HOST, ()=>{ console.log('connected, waiting for server push...'); });
let buf='';
sock.on('data', d=>{ buf+=d.toString('utf8'); console.log('PUSH:', JSON.stringify(buf)); });
sock.on('error', e=>console.log('ERR:', e.message));
sock.setTimeout(2500, ()=>{ console.log('no server push after 2.5s'); sock.destroy(); });