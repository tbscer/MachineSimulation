const net = require('net');
const http = require('http');
const HOST='127.0.0.1', PORT=9876;
// 测试原始 TCP 连接
const sock = net.connect(PORT, HOST, () => {
  console.log('TCP connected to', HOST+':'+PORT);
  sock.write('x');
  sock.end();
});
sock.on('data', d=>console.log('TCP data:', d.toString('utf8').slice(0,200)));
sock.on('error', e=>console.log('TCP error:', e.message));
sock.on('close', ()=>console.log('TCP closed'));
sock.setTimeout(3000, ()=>{console.log('TCP timeout'); sock.destroy();});