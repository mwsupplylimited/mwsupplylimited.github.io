// Render the checked-out public guides in an isolated browser on the hosted CI Mac.
// No external packages, shared browser profile, signing key or customer data.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {pathToFileURL,fileURLToPath} from 'node:url';
const repo=process.cwd();
const out=path.join(repo,'artifacts','chronograde-guides');fs.mkdirSync(out,{recursive:true});
const deadline=Date.now()+120000;
const hash=p=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const pages=[
 {path:path.join(repo,'chronograde/chronometer.html'),title:'COSC and METAS chronometer standards, explained'},
 {path:path.join(repo,'chronograde/calibers.html'),title:'Watch caliber reference — beat rates, power reserve, lift angles'}
].map(p=>({...p,sha256:hash(p.path)}));
const run=out;
const profile=fs.mkdtempSync(path.join(process.env.RUNNER_TEMP??out,'chronograde-guide-chrome-'));
const log=fs.openSync(path.join(run,'chrome.log'),'w');
const chrome=spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
 '--headless=new','--remote-debugging-port=0','--user-data-dir='+profile,
 '--no-first-run','--no-default-browser-check','--disable-extensions','--disable-background-networking','about:blank'
],{detached:true,stdio:['ignore',log,log]});
const receipt={host:'GitHub-hosted macOS',sourceCommit:process.env.GITHUB_SHA,runnerImage:process.env.ImageVersion,nodeVersion:process.version,startUTC:new Date().toISOString(),deadlineUTC:new Date(deadline).toISOString(),pid:chrome.pid,pages,cases:[]};
let ws;let next=0;const awaiting=new Map();
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const terminate=()=>{try{process.kill(-chrome.pid,'SIGTERM');}catch(e){if(e.code!=='ESRCH')throw e;}};
const watchdog=setTimeout(terminate,Math.max(0,deadline-Date.now()-8000));
function send(method,params={}){
 if(Date.now()>deadline-10000)throw Error('Cleanup reserve');
 return new Promise((resolve,reject)=>{const id=++next;const timer=setTimeout(()=>{awaiting.delete(id);reject(Error('CDP timeout '+method));},5000);awaiting.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
}
try{
 const portFile=path.join(profile,'DevToolsActivePort');
 for(let i=0;!fs.existsSync(portFile)&&i<200;i++)await sleep(100);
 if(!fs.existsSync(portFile))throw Error('Browser endpoint unavailable');
 const port=fs.readFileSync(portFile,'utf8').split('\n')[0];
 const page=await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT',signal:AbortSignal.timeout(3000)})).json();
 ws=new WebSocket(page.webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;setTimeout(()=>reject(Error('WebSocket connection timeout')),3000);});
 ws.onmessage=e=>{const v=JSON.parse(e.data);const p=awaiting.get(v.id);if(p){awaiting.delete(v.id);clearTimeout(p.timer);v.error?p.reject(Error(JSON.stringify(v.error))):p.resolve(v.result);}};
 receipt.browser=await send('Browser.getVersion');
 await send('Page.enable');
 for(const pageSource of pages){
 const source=pageSource.path;const expected=pageSource.sha256;
 for(const [mode,width,height,dark] of [['desktop-light',1280,900,false],['mobile-light',390,844,false],['desktop-dark',1280,900,true],['mobile-dark',390,844,true]]){
  const label=path.basename(source,'.html')+'-'+mode;
  await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
  await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:dark?'dark':'light'}]});
  await send('Page.navigate',{url:pathToFileURL(source).href});
  let state;
  for(let i=0;i<30;i++){state=await send('Runtime.evaluate',{expression:'document.readyState',returnByValue:true});if(state.result.value==='complete'){const check=await send('Runtime.evaluate',{expression:'document.title',returnByValue:true});if(check.result.value===pageSource.title)break;}await sleep(50);}
  if(state.result.value!=='complete')throw Error('Page not ready');
  const data=await send('Runtime.evaluate',{returnByValue:true,expression:`JSON.stringify({title:document.title,url:location.href,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,background:getComputedStyle(document.body).backgroundColor,linkColors:[...document.querySelectorAll('a')].map(e=>{let bg='rgba(0, 0, 0, 0)';for(let n=e;n&&bg==='rgba(0, 0, 0, 0)';n=n.parentElement)bg=getComputedStyle(n).backgroundColor;return {label:e.textContent,color:getComputedStyle(e).color,background:bg}}),headings:[...document.querySelectorAll('h1,h2')].map(e=>({text:e.textContent,y:e.getBoundingClientRect().y,height:e.getBoundingClientRect().height})),overflow:[...document.querySelectorAll('h1,h2,p,table,li,a')].filter(e=>{const r=e.getBoundingClientRect();return r.right>innerWidth+1||r.left< -1}).map(e=>({tag:e.tagName,text:e.textContent.slice(0,120),scrollRegion:e.closest('.table-wrap')?.id??null}))})`});
  const geometry=JSON.parse(data.result.value);
  if(geometry.title!==pageSource.title||geometry.headings.length<2||geometry.url!==pathToFileURL(source).href)throw Error('Wrong or incomplete page');
  if(geometry.background!==(dark?'rgb(18, 20, 25)':'rgb(255, 255, 255)'))throw Error('Wrong color scheme '+label);
  const luminance=color=>{const m=color.match(/^rgb\((\d+), (\d+), (\d+)\)$/);if(!m)throw Error('Unsupported rendered color '+color);const c=m.slice(1).map(x=>Number(x)/255).map(x=>x<=0.04045?x/12.92:((x+0.055)/1.055)**2.4);return c[0]*0.2126+c[1]*0.7152+c[2]*0.0722;};
  for(const link of geometry.linkColors){const values=[luminance(link.color),luminance(link.background)].sort((a,b)=>a-b);link.contrast=(values[1]+0.05)/(values[0]+0.05);if(link.contrast<4.5)throw Error('Link contrast '+label+' '+link.label);}
  const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y:0,width,height:geometry.scrollHeight,scale:1}});
  fs.writeFileSync(path.join(run,label+'.png'),Buffer.from(shot.data,'base64'));
  fs.writeFileSync(path.join(run,label+'.json'),JSON.stringify(geometry,null,2)+'\n');
  const tiles=[];
  for(let y=0;y<geometry.scrollHeight;y+=height){
   const h=Math.min(height,geometry.scrollHeight-y);
   const tile=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y,width,height:h,scale:1}});
   const name=label+'-tile-'+String(tiles.length+1).padStart(2,'0')+'.png';
   fs.writeFileSync(path.join(run,name),Buffer.from(tile.data,'base64'));
   tiles.push({name,y,height:h,sha256:hash(path.join(run,name))});
  }
  receipt.cases.push({label,...geometry,tiles,screenshotSHA256:hash(path.join(run,label+'.png'))});
  const scrollers=await send('Runtime.evaluate',{returnByValue:true,expression:`[...document.querySelectorAll('.table-wrap')].map(e=>({id:e.id,role:e.getAttribute('role'),label:e.getAttribute('aria-label'),tabIndex:e.tabIndex,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth,overflowX:getComputedStyle(e).overflowX}))`});
  const verifiedScrollRegions=[];
  for(let index=0;index<scrollers.result.value.length;index++){
   const region=scrollers.result.value[index];
   if(region.scrollWidth<=region.clientWidth)continue;
   if(!region.id||region.role!=='region'||!region.label||region.tabIndex!==0||!['auto','scroll'].includes(region.overflowX))throw Error('Inaccessible scrolling table '+label);
   await send('Runtime.evaluate',{expression:`document.querySelectorAll('.table-wrap')[${index}].focus()`});
   let state;
   for(let step=0;step<24;step++){
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowRight',code:'ArrowRight',windowsVirtualKeyCode:39});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowRight',code:'ArrowRight',windowsVirtualKeyCode:39});
    await sleep(90);
    const observed=await send('Runtime.evaluate',{returnByValue:true,expression:`(()=>{const e=document.querySelectorAll('.table-wrap')[${index}],r=e.getBoundingClientRect(),last=e.querySelector('thead th:last-child').getBoundingClientRect();return {focused:document.activeElement===e,left:e.scrollLeft,max:e.scrollWidth-e.clientWidth,lastVisible:last.left>=r.left-1&&last.right<=r.right+1,regionInViewport:r.left>=0&&r.right<=innerWidth,x:r.x,y:r.y+scrollY,width:r.width,height:Math.min(r.height,innerHeight)}})()`});
    state=observed.result.value;
    if(state.focused&&state.left>0&&state.lastVisible&&state.regionInViewport)break;
   }
   if(!state?.focused||state.left<=0||!state.lastVisible||!state.regionInViewport)throw Error('Last table column is not keyboard reachable '+label);
   const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:state.x,y:state.y,width:state.width,height:state.height,scale:1}});
   const name=label+'-'+region.id+'-keyboard-end.png';fs.writeFileSync(path.join(run,name),Buffer.from(shot.data,'base64'));
   verifiedScrollRegions.push({id:region.id,...state,screenshot:name,sha256:hash(path.join(run,name))});
   await send('Runtime.evaluate',{expression:`document.querySelectorAll('.table-wrap')[${index}].scrollLeft=0;window.scrollTo(0,0)`});
  }
  receipt.cases.at(-1).verifiedScrollRegions=verifiedScrollRegions;
  const uncontained=geometry.overflow.filter(e=>e.tag!=='TABLE'||!verifiedScrollRegions.some(r=>r.id===e.scrollRegion));
  if(geometry.scrollWidth>width||uncontained.length)throw Error('Uncontained horizontal overflow '+label);
 }
 if(hash(source)!==expected)throw Error('Source changed during rendering');
 }
 receipt.rendered=true;receipt.visualReview='PENDING';
}catch(e){receipt.error=String(e);process.exitCode=1;}
finally{
 if(ws)ws.close();terminate();
 for(let i=0;i<40&&chrome.exitCode===null&&chrome.signalCode===null;i++)await sleep(100);
 try{process.kill(-chrome.pid,0);process.kill(-chrome.pid,'SIGKILL');receipt.forcedOwnGroupCleanup=true;}catch(e){if(e.code!=='ESRCH')receipt.cleanupError=String(e);}
 clearTimeout(watchdog);fs.closeSync(log);
 receipt.endUTC=new Date().toISOString();receipt.profileRetainedOutsideArtifacts=true;
 fs.writeFileSync(path.join(run,'execution.json'),JSON.stringify(receipt,null,2)+'\n');
 console.log(JSON.stringify({rendered:receipt.rendered??false,cases:receipt.cases.length,error:receipt.error,endUTC:receipt.endUTC}));
}
