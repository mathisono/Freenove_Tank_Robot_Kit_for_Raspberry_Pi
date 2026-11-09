/* Development-only DOM/protocol regression checks for the actual app.js.
 * WebRTC, video frames, WebSocket transport, and rover hardware are mocked.
 * These tests do not validate browser layout, actual video decoding, network
 * latency, camera hardware, or physical movement. No network or GPIO is used.
 */
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const html=fs.readFileSync(path.join(root, 'rover_web/static/index.html'), 'utf8');
const source=fs.readFileSync(path.join(root, 'rover_web/static/app.js'), 'utf8');
const pause = ms=>new Promise(r=>setTimeout(r,ms));
async function harness({authenticated=true,receiverHintSupported=true}={}) {
 const dom=new JSDOM(html,{url:'http://rover.test/',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window, d=w.document, messages=[], errors=[], receivers=[];
 let socket, hidden=false, frames=true;
 let serverState={enabled:false,owner:null,control_owned:false,mode:'direct',phase:'idle',grip_closed:false,autonomy_calibrated:false,reason:'',distance_cm:70,target:{valid:true,x:.5,y:.6,radius:.1,confidence:.9},camera:{available:true,frame_age:.02},demo:true};
 w.addEventListener('error',e=>errors.push(e.error?.message||e.message));
 Object.defineProperty(d,'hidden',{get:()=>hidden});
 w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
 w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
 w.HTMLCanvasElement.prototype.getContext=()=>({clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},fillRect(){},fillText(){},measureText(x){return {width:x.length*8};}});
 w.HTMLElement.prototype.setPointerCapture=function(){};
 w.HTMLMediaElement.prototype.play=()=>Promise.resolve();
 for(const [name,value] of Object.entries({readyState:4,videoWidth:960,videoHeight:540})) Object.defineProperty(w.HTMLMediaElement.prototype,name,{get:()=>value});
 w.HTMLVideoElement.prototype.requestVideoFrameCallback=function(cb){return w.setTimeout(()=>{if(frames&&!hidden)cb(w.performance.now(),{});else this.requestVideoFrameCallback(cb);},30);};
 w.RTCRtpReceiver={getCapabilities:()=>({codecs:[{mimeType:'video/H264'}]})};
 class Peer extends w.EventTarget {
  constructor(){super();this.connectionState='new';this.iceGatheringState='complete';}
  addTransceiver(){return {setCodecPreferences(){}};}
  async createOffer(){return {sdp:'offer',type:'offer'};}
  async setLocalDescription(x){this.localDescription=x;}
  async setRemoteDescription(){this.connectionState='connected';const event=new w.Event('track');event.receiver=receiverHintSupported?{jitterBufferTarget:500}:{};receivers.push(event.receiver);event.track=new w.EventTarget();event.track.stop=()=>{};event.streams=[{getTracks:()=>[event.track]}];this.dispatchEvent(event);this.dispatchEvent(new w.Event('connectionstatechange'));}
  async getStats(){return new Map([['video',{type:'inbound-rtp',kind:'video',framesDecoded:Math.floor(w.performance.now()/33),timestamp:w.performance.now()}]]);}
  close(){this.connectionState='closed';}
 }
 w.RTCPeerConnection=Peer;
 const emit=(payload)=>socket?.dispatchEvent(new w.MessageEvent('message',{data:JSON.stringify(payload)}));
 const emitState=()=>emit({type:'state',ticket:'fresh-ticket',state:serverState});
 class WS extends w.EventTarget {
  static OPEN=1;static CONNECTING=0;
  constructor(){super();this.readyState=0;this.bufferedAmount=0;socket=this;w.setTimeout(()=>{this.readyState=1;this.dispatchEvent(new w.Event('open'));emitState();},5);this.timer=w.setInterval(emitState,100);}
  send(raw){const m=JSON.parse(raw);messages.push(m);if(m.type==='ping'){emit({type:'pong',sent_at:m.sent_at});return;}if(m.type==='action'){
   if(m.action==='enable')serverState={...serverState,enabled:true,owner:'this-browser',control_owned:true};
   if(['disable','stop'].includes(m.action))serverState={...serverState,enabled:false,owner:null,control_owned:false,phase:serverState.grip_closed?'holding':'stopped'};
   if(m.action==='grab')serverState={...serverState,phase:'acquiring'};
   if(m.action==='release')serverState={...serverState,phase:'releasing',grip_closed:false};
   w.setTimeout(emitState,0);
  }}
  close(){this.readyState=3;w.clearInterval(this.timer);this.dispatchEvent(new w.Event('close'));}
 }
 w.WebSocket=WS;
 w.fetch=async(path,opts)=>({ok:true,status:200,json:async()=>{
   if(path==='/api/status')return {authenticated,demo:authenticated,state:serverState};
   if(path==='/api/login'){authenticated=true;return {authenticated:true};}
   return {sdp:'answer',type:'answer'};
 }});
 w.eval(source);
 return {dom,w,d,messages,errors,receivers,emit,emitState,setState(x){serverState={...serverState,...x};emitState();},setFrames(x){frames=x;},setHidden(x){hidden=x;d.dispatchEvent(new w.Event('visibilitychange'));},key(type,code){d.body.dispatchEvent(new w.KeyboardEvent(type,{code,bubbles:true}));},pointer(type,button,id=1){const event=new w.Event(type,{bubbles:true,cancelable:true});Object.assign(event,{pointerId:id,pointerType:'touch',button:0});button.dispatchEvent(event);},close(){dom.window.close();}};
}
(async()=>{
 const results=[];
 const h=await harness();await pause(350);
 assert.equal(h.d.querySelector('#liveLabel').textContent,'LIVE');assert.equal(h.d.querySelector('#enableButton').disabled,false);results.push('fresh rendered video gates control enable');
 assert.equal(h.d.querySelector('#grabButton').disabled,true);assert.match(h.d.querySelector('#retrievalDetail').textContent,/calibration/i);results.push('uncalibrated pickup is visibly unavailable');
 h.setState({phase:'error',reason:'Ultrasonic range is stale'});assert.equal(h.d.querySelector('#retrievalDetail').textContent,'Ultrasonic range is stale');h.setState({phase:'stopped',reason:'Operator stop'});assert.equal(h.d.querySelector('#retrievalDetail').textContent,'Operator stop');h.setState({phase:'idle',reason:''});results.push('specific fault and stop reasons take priority over calibration advice');
 assert.equal(h.receivers[0].jitterBufferTarget,0);
 h.d.querySelector('#enableButton').click();await pause(100);assert.equal(h.d.querySelector('#controlState').textContent,'ENABLED');
 h.key('keydown','KeyW');await pause(130);let drives=h.messages.filter(x=>x.type==='drive');assert(drives.filter(x=>x.throttle>0).length>=3);assert(drives.every(x=>x.ticket==='fresh-ticket'));h.key('keyup','KeyW');await pause(70);drives=h.messages.filter(x=>x.type==='drive');assert.equal(drives.at(-1).throttle,0);const afterRelease=drives.length;await pause(150);assert.equal(h.messages.filter(x=>x.type==='drive').length,afterRelease);results.push('25 Hz hold-to-drive, zero on release, no idle drive traffic');
 h.setState({autonomy_calibrated:true,phase:'idle'});h.d.querySelector('#grabButton').click();await pause(100);assert.equal(h.d.querySelector('#grabButton').getAttribute('aria-pressed'),'true');assert.equal(h.d.querySelector('#grabButton').disabled,true);const afterGrab=h.messages.filter(x=>x.type==='drive').length;await pause(180);assert.equal(h.messages.filter(x=>x.type==='drive').length,afterGrab);results.push('Grab latch is not cancelled by idle joystick');
 h.w.dispatchEvent(new h.w.Event('blur'));await pause(100);assert.equal(h.d.querySelector('#controlState').textContent,'DISABLED');assert.equal(h.messages.filter(x=>x.type==='action').at(-1).action,'stop');results.push('focus loss stops and requires deliberate re-enable');
 h.setState({phase:'idle'});h.d.querySelector('#enableButton').click();await pause(100);const right=h.d.querySelector('[data-drive="right"]');h.pointer('pointerdown',right);await pause(100);h.pointer('pointercancel',h.w);await pause(100);assert.equal(h.messages.filter(x=>x.type==='drive').at(-1).steering,0);results.push('pointer cancellation releases drive');
 h.setFrames(false);await pause(1100);assert.equal(h.d.querySelector('#controlState').textContent,'DISABLED');h.setFrames(true);await pause(200);assert.equal(h.d.querySelector('#controlState').textContent,'DISABLED');results.push('stalled rendered video stops and never auto-resumes controls');
 h.setState({phase:'holding',grip_closed:true});h.setFrames(false);await pause(1000);assert.equal(h.d.querySelector('#releaseButton').disabled,false);h.d.querySelector('#releaseButton').click();assert.equal(h.messages.filter(x=>x.type==='action').at(-1).action,'release');results.push('explicit release works with controls and video disabled');
 h.setFrames(true);h.setState({phase:'idle',grip_closed:false,owner:'other-browser',control_owned:false,enabled:true});await pause(200);assert.equal(h.d.querySelector('#enableButton').disabled,true);assert.equal(h.d.querySelector('#releaseButton').disabled,true);assert.match(h.d.querySelector('#footerStatus').textContent,/another operator/);results.push('another operator ownership disables drive and release');
 const search=[...h.d.querySelectorAll('button')].find(b=>b.textContent.includes('Search Mode'));assert(search.disabled);assert.equal(search.getAttribute('aria-disabled'),'true');results.push('Search Mode remains disabled');
 assert.deepEqual(h.errors,[]);h.close();
 const login=await harness({authenticated:false,receiverHintSupported:false});await pause(100);assert(login.d.querySelector('#loginDialog').open);login.d.querySelector('#accessKey').value='test-access-key';login.d.querySelector('#loginForm').dispatchEvent(new login.w.Event('submit',{bubbles:true,cancelable:true}));await pause(200);assert(!login.d.querySelector('#loginDialog').open);assert.equal(login.d.querySelector('#accessKey').value,'');assert.equal(login.w.localStorage.length,0);assert.deepEqual(login.errors,[]);assert.equal(Object.hasOwn(login.receivers[0],'jitterBufferTarget'),false);login.close();results.push('access key login clears field and does not persist credentials');results.push('minimal jitter buffer hint is feature-tested on mocked receivers');
 console.log(JSON.stringify({passed:results.length,checks:results},null,2));
})().catch(e=>{console.error(e);process.exit(1);});
