const http = require('http');
const HOST='127.0.0.1', PORT=9876;
// 探测常见 HTTP 端点
const paths=['/','/sse','/mcp','/message','/initialize','/health'];
function probe(p){
  return new Promise(res=>{
    const req=http.request({host:HOST,port:PORT,path:p,method:'GET',timeout:3000}, r=>{
      let body='';
      r.on('data',d=>body+=d);
      r.on('end',()=>res({p,code:r.statusCode,ct:r.headers['content-type'],body:body.slice(0,150)}));
    });
    req.on('timeout',()=>{req.destroy();res({p,code:'TIMEOUT',ct:'',body:''});});
    req.on('error',e=>res({p,code:'ERR:'+e.message,ct:'',body:''}));
    req.end();
  });
}
(async()=>{
  for(const p of paths){ const r=await probe(p); console.log(JSON.stringify(r)); }
})();