// Playlist Downloader — Cloudflare Worker
// GitHub Actions authenticates with short-lived GitHub OIDC JWTs; no GitHub token is stored in the site.
const FRONTEND = __FRONTEND__;
const ALLOWED_REPO = "AlexandreMoiteiro/playlist-downloader";
const EXPECTED_REPO_ID = "1460880947";
const PLAYLIST = /^https:\/\/open\.spotify\.com\/playlist\/([A-Za-z0-9]{22})(?:[/?#].*)?$/;
const JOB_ID = /^[0-9a-f]{32}$/;
const MAX_UPLOAD = 1500 * 1024 * 1024;
const H = {"content-type":"application/json; charset=utf-8","cache-control":"no-store","x-content-type-options":"nosniff"};
function result(v, code=200){return new Response(JSON.stringify(v),{status:code,headers:H})}
function now(){return Math.floor(Date.now()/1000)}
function b64(s){const str=s.replace(/-/g,"+").replace(/_/g,"/");return atob(str.padEnd(Math.ceil(str.length/4)*4,"="))}
function bytes(s){return Uint8Array.from(b64(s),c=>c.charCodeAt(0))}
async function authorized(request){
  const auth=request.headers.get("authorization")||"";
  if(!auth.startsWith("Bearer "))return false;
  try{
    const [h,p,s]=auth.slice(7).split(".");
    if(!h||!p||!s)return false;
    const hdr=JSON.parse(b64(h));const claims=JSON.parse(b64(p));
    if(hdr.alg!=="RS256"||!hdr.kid)return false;
    const t=now();
    if(claims.iss!=="https://token.actions.githubusercontent.com"||
      claims.repository!==ALLOWED_REPO||
      String(claims.repository_id)!==EXPECTED_REPO_ID||
      claims.ref!=="refs/heads/main"||
      !String(claims.workflow_ref||"").startsWith(ALLOWED_REPO+"/.github/workflows/download.yml@refs/heads/main")||
      !([].concat(claims.aud||[])).includes("playlist-studio")||
      !claims.exp||claims.exp<t||claims.exp>t+600||claims.nbf&&claims.nbf>t+30||
      claims.iat&&claims.iat>t+30)return false;
    const jwks=await fetch("https://token.actions.githubusercontent.com/.well-known/jwks",{cf:{cacheTtl:3600,cacheEverything:true},signal:AbortSignal.timeout(8000)});
    if(!jwks.ok)return false;
    const keys=await jwks.json(),key=keys.keys.find(k=>k.kid===hdr.kid&&k.kty==="RSA");
    if(!key)return false;
    const cryptoKey=await crypto.subtle.importKey("jwk",key,{name:"RSASSA-PKCS1-v1_5",hash:"SHA-256"},false,["verify"]);
    return crypto.subtle.verify("RSASSA-PKCS1-v1_5",cryptoKey,bytes(s),new TextEncoder().encode(h+"."+p));
  }catch(e){return false}
}
async function getJob(env,id){return env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first()}
async function housekeeping(env){
  const expired=await env.DB.prepare("SELECT id,archive_key FROM jobs WHERE state='ready' AND updated_at < ? LIMIT 10").bind(now()-86400).all();
  for(const job of expired.results||[]){
    if(job.archive_key)await env.FILES.delete(job.archive_key);
    await env.DB.prepare("UPDATE jobs SET state='expired' WHERE id=?").bind(job.id).run();
  }
  await env.DB.prepare("UPDATE jobs SET state='failed',progress_phase='failed',error='O processamento excedeu o tempo limite.' WHERE state='running' AND started_at < ?").bind(now()-9000).run();
}
async function enqueue(request,env){
  let body;try{body=await request.json()}catch{return result({error:"Pedido inválido."},400)}
  const url=String(body?.url||"").trim(),m=url.match(PLAYLIST);
  if(!m)return result({error:"Introduz um link válido de playlist do Spotify."},400);
  const n=now();
  const existing=await env.DB.prepare("SELECT id,state FROM jobs WHERE playlist_id=? AND state IN ('queued','running','ready') AND created_at > ? ORDER BY created_at DESC LIMIT 1").bind(m[1],n-86400).first();
  if(existing)return result({id:existing.id,status:existing.state},existing.state==="ready"?200:202);
  const stats=await env.DB.prepare("SELECT count(*) AS total FROM jobs WHERE created_at > ?").bind(n-86400).first();
  const queued=await env.DB.prepare("SELECT count(*) AS total FROM jobs WHERE state IN ('queued','running')").first();
  if(Number(stats?.total||0)>=8)return result({error:"O limite diário de processamento foi atingido. Tenta mais tarde."},429);
  if(Number(queued?.total||0)>=2)return result({error:"Existem outros downloads em curso. Tenta mais tarde."},429);
  const id=crypto.randomUUID().replace(/-/g,"");
  await env.DB.prepare("INSERT INTO jobs(id,playlist_id,state,created_at,updated_at) VALUES(?,?,?,?,?)").bind(id,m[1],"queued",n,n).run();
  return result({id,status:"queued"},202);
}
async function status(request,env,id){
  const job=await getJob(env,id);
  if(!job)return result({error:"Pedido não encontrado."},404);
  const state=job.state,phase=job.progress_phase||"queued";
  const downloaded=Math.max(0,Number(job.tracks_downloaded)||0);
  const sent=Math.max(0,Number(job.transfer_done)||0);
  const total=Math.max(0,Number(job.transfer_total)||0);
  const percent=state==="ready"?100:phase==="uploading"&&total>0?Math.min(99,Math.floor(sent/total*100)):null;
  let message="A aguardar processamento no GitHub…";
  if(state==="running"){
    if(phase==="packaging")message="A reunir os MP3 e a criar o ZIP…";
    else if(phase==="uploading")message="A enviar o ZIP para download…";
    else message=downloaded>0?downloaded+" MP3 descarregados. A obter as restantes músicas…":"A procurar e descarregar as músicas…";
  }else if(state==="ready")message="ZIP pronto para descarregar.";
  else if(state==="failed")message=job.error||"Não foi possível processar a playlist.";
  else if(state==="expired")message="O download expirou.";
  let tracks=[];
  try{tracks=JSON.parse(job.track_details||"[]")}catch{}
  if(!Array.isArray(tracks))tracks=[];
  const track_total=tracks.length, track_done=tracks.filter(t=>t.state==="done").length, track_failed=tracks.filter(t=>t.state==="failed").length;
  return result({state,phase,downloaded,transfer_done:sent,transfer_total:total,percent,message,tracks,track_total,track_done,track_failed});
}
async function progress(request,env,id){
  const job=await getJob(env,id);
  if(!job||job.state!=="running")return result({error:"Job inválido."},409);
  let body;
  try{body=await request.json()}catch{return result({error:"Dados inválidos."},400)}
  const phase=String(body.phase||"");
  if(!["downloading","packaging","uploading"].includes(phase))return result({error:"Fase inválida."},400);
  const count=Number(body.downloaded??job.tracks_downloaded??0);
  const total=Number(body.total_bytes??job.transfer_total??0);
  if(!Number.isSafeInteger(count)||count<0||count>20000||!Number.isSafeInteger(total)||total<0||total>MAX_UPLOAD)return result({error:"Valores de progresso inválidos."},400);
  await env.DB.prepare("UPDATE jobs SET progress_phase=?,tracks_downloaded=?,transfer_total=?,updated_at=? WHERE id=? AND state='running'").bind(phase,count,total,now(),id).run();
  if(body.tracks!==undefined){
    const states=new Set(["pending","searching","downloading","converting","metadata","verifying","done","failed"]);
    if(!Array.isArray(body.tracks)||body.tracks.length>10000)return result({error:"Lista de faixas inválida."},400);
    const tracks=body.tracks.map(t=>({id:String(t?.id||"").slice(0,64),title:String(t?.title||"").slice(0,220),artist:String(t?.artist||"").slice(0,220),state:states.has(t?.state)?t.state:"pending",progress:Math.max(0,Math.min(100,Math.round(Number(t?.progress)||0)))}));
    const snapshot=JSON.stringify(tracks);
    if(new TextEncoder().encode(snapshot).length>1500000)return result({error:"Lista de faixas demasiado grande."},413);
    await env.DB.prepare("UPDATE jobs SET track_details=? WHERE id=? AND state='running'").bind(snapshot,id).run();
  }
  return result({ok:true});
}

async function download(env,id){
  const job=await getJob(env,id);
  if(!job||job.state!=="ready"||!job.archive_key)return result({error:"O ZIP não está disponível."},404);
  const item=await env.FILES.get(job.archive_key);
  if(!item)return result({error:"ZIP não encontrado."},404);
  return new Response(item.body,{headers:{
    "content-type":"application/zip",
    "content-disposition":'attachment; filename="playlist.zip"',
    "cache-control":"private, no-store",
    "x-content-type-options":"nosniff",
    ...(item.size?{"content-length":String(item.size)}:{})
  }});
}
async function dequeue(env){
  await housekeeping(env);
  const job=await env.DB.prepare("UPDATE jobs SET state='running',progress_phase='downloading',started_at=?,updated_at=? WHERE id = (SELECT id FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1) AND state='queued' RETURNING id,playlist_id").bind(now(),now()).first();
  return result({job:job?{id:job.id,url:"https://open.spotify.com/playlist/"+job.playlist_id}:null});
}
async function uploadStart(env,id){
  const job=await getJob(env,id);
  if(!job||job.state!=="running")return result({error:"Job inválido."},409);
  if(job.upload_id)return result({ok:true,upload_id:job.upload_id});
  const key="archives/"+id+".zip";
  const multipart=await env.FILES.createMultipartUpload(key,{httpMetadata:{contentType:"application/zip"}});
  await env.DB.prepare("UPDATE jobs SET archive_key=?, upload_id=?,progress_phase='uploading' WHERE id=? AND state='running'").bind(key,multipart.uploadId,id).run();
  return result({ok:true});
}
async function uploadPart(request,env,id,partNumber){
  const job=await getJob(env,id);
  if(!job||job.state!=="running"||!job.upload_id)return result({error:"Upload não iniciado."},409);
  const n=Number(partNumber),size=Number(request.headers.get("content-length")||0);
  if(!Number.isInteger(n)||n<1||n>200||size<1||size>12*1024*1024)return result({error:"Parte inválida."},413);
  if(!request.body)return result({error:"Corpo vazio."},400);
  const obj=env.FILES.resumeMultipartUpload(job.archive_key,job.upload_id);
  const part=await obj.uploadPart(n,request.body);
  await env.DB.prepare("INSERT INTO job_parts(job_id,number,etag,size) VALUES(?,?,?,?) ON CONFLICT(job_id,number) DO UPDATE SET etag=excluded.etag,size=excluded.size").bind(id,n,part.etag,size).run();
  await env.DB.prepare("UPDATE jobs SET transfer_done=(SELECT COALESCE(SUM(size),0) FROM job_parts WHERE job_id=?),updated_at=? WHERE id=? AND state='running'").bind(id,now(),id).run();
  return result({ok:true,part:n});
}
async function uploadFinish(env,id){
  const job=await getJob(env,id);
  if(!job||job.state!=="running"||!job.upload_id)return result({error:"Job inválido."},409);
  const found=await env.DB.prepare("SELECT number,etag,size FROM job_parts WHERE job_id=? ORDER BY number").bind(id).all();
  const parts=found.results||[];
  if(!parts.length||parts.length>200||parts.some((p,i)=>p.number!==i+1))return result({error:"Partes em falta."},409);
  const total=parts.reduce((sum,p)=>sum+Number(p.size),0);
  if(total>MAX_UPLOAD)return result({error:"ZIP excede o tamanho máximo."},413);
  const obj=env.FILES.resumeMultipartUpload(job.archive_key,job.upload_id);
  await obj.complete(parts.map(x=>({partNumber:x.number,etag:x.etag})));
  await env.DB.prepare("UPDATE jobs SET state='ready',progress_phase='ready',transfer_done=?,transfer_total=?,updated_at=?,total_size=? WHERE id=? AND state='running'").bind(total,total,now(),total,id).run();
  return result({ok:true});
}
async function markError(request,env,id){
  const job=await getJob(env,id);
  if(!job||job.state!=="running")return result({error:"Job inválido."},409);
  let body={};try{body=await request.json()}catch{}
  const msg=String(body.error||"Não foi possível descarregar os MP3.").slice(0,360);
  if(job.upload_id)try{await env.FILES.resumeMultipartUpload(job.archive_key,job.upload_id).abort()}catch{}
  await env.DB.prepare("UPDATE jobs SET state='failed',progress_phase='failed',error=?,updated_at=? WHERE id=?").bind(msg,now(),id).run();
  return result({ok:true});
}
export default {
  async fetch(request,env){
    try{
      const u=new URL(request.url),path=u.pathname,method=request.method;
      if(path==="/"&&method==="GET")return new Response(FRONTEND,{headers:{"content-type":"text/html; charset=utf-8","cache-control":"public,max-age=120","x-content-type-options":"nosniff","referrer-policy":"no-referrer","x-frame-options":"DENY"}});
      if(path==="/health"&&method==="GET")return result({ok:true});
      if(path==="/api/jobs"&&method==="POST")return enqueue(request,env);
      const view=path.match(/^\/api\/jobs\/([0-9a-f]{32})(?:\/(download))?$/);
      if(view&&method==="GET")return view[2]?download(env,view[1]):status(request,env,view[1]);
      if(path.startsWith("/api/processor/")){
        if(!(await authorized(request)))return result({error:"Acesso negado."},401);
        if(path==="/api/processor/next"&&method==="GET")return dequeue(env);
        const m=path.match(/^\/api\/processor\/([0-9a-f]{32})\/(start|finish|error|progress|part\/([1-9][0-9]*))$/);
        if(m){
          if(m[2]==="start"&&method==="POST")return uploadStart(env,m[1]);
          if(m[2]==="finish"&&method==="POST")return uploadFinish(env,m[1]);
          if(m[2]==="error"&&method==="POST")return markError(request,env,m[1]);
          if(m[2]==="progress"&&method==="POST")return progress(request,env,m[1]);
          if(m[3]&&method==="PUT")return uploadPart(request,env,m[1],m[3]);
        }
      }
      return result({error:"Não encontrado."},404);
    }catch(e){
      console.error(JSON.stringify({error:String(e),stack:e?.stack?.slice(0,600)}));
      return result({error:"Ocorreu um erro no servidor."},500);
    }
  }
};