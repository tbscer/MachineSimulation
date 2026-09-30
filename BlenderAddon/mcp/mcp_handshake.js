const net = require('net');
const HOST='127.0.0.1', PORT=9876;
const sock = net.connect(PORT, HOST, ()=>{
  const msg={jsonrpc:"2.0",id:1,method:"initialize",params:{protocolVersion:"2024-11-05",capabilities:{},clientInfo:{name:"qoder-probe",version:"1.0"}}};
  sock.write(JSON.stringify(msg)+'\n');
});
let buf='';
sock.on('data', d=>{
  buf+=d.toString('utf8');
  // 按行分割
  const lines=buf.split('\n');
  buf=lines.pop();
  for(const l of lines){ if(l.trim()) console.log('RESP:', l); }
});
sock.on('error', e=>console.log('ERR:', e.message));
sock.setTimeout(6000, ()=>{console.log('done (timeout)'); sock.destroy();});