// Pinned-version native text agent. Input arrives as JSON data, never evaluated.
// Does not type text, paste, click Send, reuse a historical message, or bypass confirmations.
const base = Process.mainModule.base;
for(const item of PROFILE.functions) {
  const got=Array.from(new Uint8Array(base.add(item.rva).readByteArray(16)))
    .map(x=>x.toString(16).padStart(2,'0')).join('');
  if(got!==item.bytes) throw new Error('function_fingerprint_mismatch');
}
const expectedVtable = base.add(0xa6a9e80);
const text=CONFIG.text;
const target=CONFIG.chat_id;
if(Process.id!==CONFIG.pid || Process.mainModule.path!=='/opt/wechat/wechat') throw new Error('wrong_client');
let byteLength=0;
for(const ch of text){const c=ch.codePointAt(0);byteLength+=c<0x80?1:c<0x800?2:c<0x10000?3:4;}
if(byteLength!==CONFIG.byte_length || byteLength<1 || byteLength>4096 || text.includes('\0')) throw new Error('invalid_text');
const state = {stage:'observing',calls:0,mainThread:Process.id};
let view = null, pending = null;


function cxx(p) {
  const first=p.readU8(), n=(first&1)?p.add(8).readU64().toNumber():first>>>1;
  if(n>4096) throw new Error('string_too_large');
  return {n, p:(first&1)?p.add(16).readPointer():p.add(1)};
}
function ownView(p) {
  try {
    if(!p.readPointer().equals(expectedVtable)) return false;
    const s=cxx(p.add(0x250));
    return s.n===target.length && s.p.readUtf8String(s.n)===target
      && p.add(0x228).readPointer().add(0x10).readPointer().equals(p)
      && !p.add(0x288).readPointer().isNull();
  } catch (_) { return false; }
}

function native(rva, result, args) {return new NativeFunction(base.add(rva),result,args);}
const allocate=native(0xa674ee0,'pointer',['ulong']);
const construct=native(0x4a04150,'void',['pointer']);
const makeString=native(0x450e6a0,'void',['pointer','pointer','ulong']);
const append=native(0x4969970,'void',['pointer','pointer']);
const releasePair=native(0x49770c0,'void',['pointer']);
const releaseList=native(0x4958730,'void',['pointer']);
const submit=native(0x497d4c0,'void',['pointer','int','pointer']);
const glib=Process.enumerateModules().find(m=>m.name.startsWith('libglib-2.0.so'));
if(!glib) throw new Error('glib_module_unavailable');
const idle=new NativeFunction(glib.getExportByName('g_idle_add_full'),'uint',
  ['int','pointer','pointer','pointer']);
const removeSource=new NativeFunction(glib.getExportByName('g_source_remove'),'int',['uint']);
let sourceId=0;
let pinned=false;

function execute(mode) {
  state.stage='checking';
  if(Process.getCurrentThreadId()!==Process.id || !view || !ownView(view)) {
    state.stage=Process.getCurrentThreadId()!==Process.id?'wrong_thread':'rejected_context';return;
  }
  let pair=null,list=null,listReady=false,pairReady=false;
  try {
    // Native shared_ptr control block + TextMessageSendSource, size verified from the factory.
    const block=allocate(0x610);
    construct(block);
    pair=Memory.alloc(16); pair.writePointer(block.add(24)); pair.add(8).writePointer(block);
    pairReady=true;
    const source=block.add(24);
    if(!source.readPointer().equals(base.add(0xa8a10c8))) throw new Error('wrong_source_vtable');
    source.add(0xe4).writeU32(1);
    const bytes=Memory.allocUtf8String(text);
    makeString(source.add(0x5c8),bytes,byteLength);
    const body=cxx(source.add(0x5c8));
    if(body.n!==byteLength || body.p.readUtf8String(body.n)!==text) throw new Error('body_mismatch');
    list=Memory.alloc(Process.pointerSize);list.writePointer(base.add(0xacb47d0));
    append(list,pair);listReady=true;
    const data=list.readPointer(), begin=data.add(8).readS32(), end=data.add(12).readS32();
    if(end-begin!==1) throw new Error('wrong_list_length');
    state.construction={vtableMatched:true,type:source.add(0xe4).readU32(),bodyMatched:true,
      listCount:1,tid:Process.getCurrentThreadId(),uuidPresent:cxx(source.add(0x580)).n>0};
    if(mode==='send') {
      if(state.cancelled || !ownView(view) || state.calls!==0) throw new Error('send_guard');
      state.calls=1;state.stage='calling';
      submit(view.add(0x228).readPointer(),3,list);
      state.stage='returned_unconfirmed';
    } else {state.stage='constructed_only';}
  } catch(e) {
    const known=new Set(['wrong_source_vtable','body_mismatch','wrong_list_length','send_guard']);
    state.error=known.has(e.message)?e.message:'native_operation_error';state.stage='error';
  } finally {
    if(listReady) releaseList(list);
    if(pairReady) releasePair(pair);
  }
}

// Use GLib's supported one-shot source API; do not intercept the event-loop locks.
const callback=new NativeCallback(function(){
  sourceId=0;
  if(pending!==null){const mode=pending;pending=null;execute(mode);}
  return 0;
},'int',['pointer']);
// A queued native callback must outlive the controller process/session.
// GLib releases this pin only when the source and its callback are destroyed.
const sourceDestroyed=new NativeCallback(function(){
  if(pinned){pinned=false;Script.unpin();}
},'void',['pointer']);
rpc.exports.discover=()=>{
  const pattern=Array.from(new Uint8Array(Memory.alloc(Process.pointerSize)
    .writePointer(expectedVtable).readByteArray(Process.pointerSize)))
    .map(x=>x.toString(16).padStart(2,'0')).join(' ');
  const found=new Map();let scanned=0;
  for(const r of Process.enumerateRanges({protection:'rw-',coalesce:true})){
    if(r.file || r.size>128*1024*1024 || scanned+r.size>512*1024*1024) continue;
    scanned+=r.size;
    try{for(const hit of Memory.scanSync(r.base,r.size,pattern)){
      if(ownView(hit.address))found.set(hit.address.toString(),hit.address);
    }}catch(_){}
  }
  view=found.size===1?Array.from(found.values())[0]:null;
  return {matches:found.size,scannedMiB:Math.round(scanned/1024/1024),viewFound:!!view};
};
rpc.exports.status=()=>({...state,viewFound:!!view});
rpc.exports.arm=()=>{
  const mode='send';
  if(!['construct','send'].includes(mode) || state.stage!=='observing' || !view)
    return {accepted:false,...state,viewFound:!!view};
  pending=mode;state.stage='armed';Script.pin();pinned=true;
  sourceId=idle(200,callback,ptr(0),sourceDestroyed);
  return {accepted:sourceId>0};
};
rpc.exports.cancel=()=>{
  state.cancelled=true;pending=null;
  if(sourceId){removeSource(sourceId);sourceId=0;}
  return {cancelled:true,calls:state.calls,stage:state.stage};
};
