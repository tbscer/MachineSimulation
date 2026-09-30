const net = require('net');
const HOST='127.0.0.1', PORT=9876;
const sock = net.connect(PORT, HOST, ()=>{
  const cmd={type:"get_scene_info", params:{}};
  sock.write(JSON.stringify(cmd));
  console.log('SENT:', JSON.stringify(cmd));
});
let buf='';
sock.on('data', d=>{
  buf+=d.toString('utf8');
  try { JSON.parse(buf); console.log('RESPONSE RECEIVED:'); console.log(buf); sock.destroy(); }
  catch(e){ /* 未完整 */ }
});
sock.on('error', e=>console.log('ERR:', e.message));
sock.setTimeout(20000, ()=>{ console.log('TIMEOUT, partial:', buf.slice(0,500)); sock.destroy(); });