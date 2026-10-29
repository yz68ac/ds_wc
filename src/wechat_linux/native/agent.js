// Fixed-version explicit-recipient service sender. No UI access, hooks or heap scan.
const base=Process.mainModule.base;
if(Process.arch!=='x64'||Process.pointerSize!==8||Process.id!==CONFIG.pid||Process.mainModule.path!=='/opt/wechat/wechat'||PROFILE.backend!=='service_v1')throw Error('native_profile_mismatch');
if(!/^(filehelper|wxid_[A-Za-z0-9_-]{1,120}|[A-Za-z0-9_-]{1,128}@chatroom)$/.test(CONFIG.chat_id))throw Error('native_invalid_request');
const imageSend=CONFIG.op==='send_image';
let byteLength=0;
if(imageSend){
  if(!PROFILE.send_image||typeof CONFIG.image_path!=='string'||!CONFIG.image_path.startsWith('/')||CONFIG.image_path.includes('\0')||!Number.isInteger(CONFIG.byte_length)||CONFIG.byte_length<1||CONFIG.byte_length>8388608)throw Error('native_invalid_request');
}else{
  if(typeof CONFIG.text!=='string')throw Error('native_invalid_request');
  for(const ch of CONFIG.text){const c=ch.codePointAt(0);byteLength+=c<128?1:c<2048?2:c<65536?3:4;}
  if(byteLength!==CONFIG.byte_length||byteLength<1||byteLength>4096||CONFIG.text.includes('\0'))throw Error('native_invalid_request');
}
for(const f of PROFILE.functions){
  const got=Array.from(new Uint8Array(base.add(f.rva).readByteArray(16))).map(x=>x.toString(16).padStart(2,'0')).join('');
  if(got!==f.bytes)throw Error('native_profile_mismatch');
}
const state={stage:'observing',calls:0,recipient_verified:false,mainThread:Process.id};
let pending=false,sourceId=0,pinned=false;
function native(rva,ret,args){return new NativeFunction(base.add(rva),ret,args);}
const alloc=native(0xa674ee0,'pointer',['ulong']);
const construct=native(0x4a04150,'void',['pointer']);
const makeString=native(0x450e6a0,'void',['pointer','pointer','ulong']);
const releasePair=native(0x49770c0,'void',['pointer']);
const prepare=native(0x7129c50,'void',['pointer','pointer','pointer']);
const batch=native(0x7067550,'pointer',['pointer','pointer','pointer','int']);
const location=native(0x4540610,'void',['pointer','pointer','pointer','int','pointer']);
const subscribe=native(0x4709f00,'pointer',['pointer','pointer','pointer','pointer','pointer','pointer']);
const destroySubscription=native(0x45cefc0,'void',['pointer']);
const destroyString=native(0x4539690,'void',['pointer']);
function zero(n){const p=Memory.alloc(n);p.writeByteArray(new Uint8Array(n));return p;}
function cxx(p){const f=p.readU8(),n=(f&1)?p.add(8).readU64().toNumber():f>>>1;if(n>4096)throw Error('invalid_string');return {n,p:(f&1)?p.add(16).readPointer():p.add(1)};}
function string(p){const s=cxx(p);return s.p.readUtf8String(s.n);}
function put(p,s){let n=0;for(const ch of s){const c=ch.codePointAt(0);n+=c<128?1:c<2048?2:c<65536?3:4;}const buf=Memory.allocUtf8String(s);makeString(p,buf,n);}
function services(){
  const kernel=base.add(0xacf6710).readPointer(),account=kernel.add(0x68).readPointer(),registry=account.add(0x18).readPointer();
  const count=registry.add(0x18).readU64().toNumber();if(count<1||count>512)throw Error('registry_bounds');
  const found={};let node=registry.add(0x10).readPointer(),seen=new Set();
  for(let i=0;!node.isNull()&&i<count;i++){
    if(seen.has(node.toString()))throw Error('registry_cycle');seen.add(node.toString());
    const obj=node.add(0x28).readPointer(),control=node.add(0x30).readPointer();
    if(!obj.isNull()&&!control.isNull()){
      const vt=obj.readPointer();
      for(const [name,rva] of [['metadata',0xa92c848],['batch',0xa9222a0]])if(vt.equals(base.add(rva))){if(found[name])throw Error('duplicate_service');found[name]=obj;}
    }
    node=node.readPointer();
  }
  if(!found.metadata||!found.batch)throw Error('services_not_found');
  return {...found,count};
}
function execute(){
  if(Process.getCurrentThreadId()!==Process.id){state.stage='wrong_thread';return;}
  let pair=null,task=null,taskReady=false,sub=null,subReady=false;
  try{
    state.stage='checking';const svc=services();state.registry_count=svc.count;
    pair=zero(16);let src,block;
    if(imageSend){
      const inputPath=zero(24),extra=zero(24);
      try{
        put(inputPath,CONFIG.image_path);
        native(PROFILE.send_image.factory_rva,'pointer',['pointer','int','pointer','pointer'])(pair,3,inputPath,extra);
      }finally{destroyString(inputPath);}
      src=pair.readPointer();block=pair.add(8).readPointer();
      if(src.isNull()||block.isNull()||!src.readPointer().equals(base.add(PROFILE.send_image.source_vtable_rva))||src.add(0xe4).readU32()!==3)throw Error('image_source_fields');
      if(string(src.add(PROFILE.send_image.path_offset))!==CONFIG.image_path||src.add(PROFILE.send_image.size_offset).readU64().toNumber()!==CONFIG.byte_length)throw Error('image_source_fields');
      put(src.add(0x90),CONFIG.chat_id);state.image_source_verified=true;state.message_type=3;
    }else{
      block=alloc(0x610);construct(block);
      src=block.add(24);pair.writePointer(src);pair.add(8).writePointer(block);
      if(!src.readPointer().equals(base.add(0xa8a10c8)))throw Error('source_vtable');
      src.add(0xe4).writeU32(1);put(src.add(0x90),CONFIG.chat_id);put(src.add(0x5c8),CONFIG.text);
      if(string(src.add(0x5c8))!==CONFIG.text)throw Error('source_fields');
    }
    if(string(src.add(0x90))!==CONFIG.chat_id||!cxx(src.add(0x580)).n)throw Error('source_fields');
    const vec=zero(24);vec.writePointer(pair);vec.add(8).writePointer(pair.add(16));vec.add(16).writePointer(pair.add(16));
    task=zero(80);batch(task,svc.batch,vec,1);taskReady=true;
    const callable=task.add(32).readPointer();
    if(callable.isNull()||!callable.readPointer().equals(base.add(0xa924968)))throw Error('task_vtable');
    const begin=callable.add(8).readPointer(),end=callable.add(16).readPointer();
    if(!end.sub(begin).equals(ptr(16))||!begin.readPointer().equals(src)||!begin.add(8).readPointer().equals(block))throw Error('task_sources');
    state.recipient_verified=true;
    {
      if(state.calls!==0||state.cancelled)throw Error('send_guard');
      // Match the normal metadata step. It is not itself a sender.
      prepare(svc.metadata,src.add(0x90),vec);
      // Source-location fields are borrowed by native async diagnostics. Use only
      // fixed module rodata/code addresses, which outlive script detachment.
      const loc=zero(32);location(loc,base.add(0xd9729a),base.add(0xdd47d5),0x227,base.add(0x708e960));
      sub=zero(40);const success=zero(40),failure=zero(40),done=zero(40);
      state.calls=1;state.stage='submitting_unknown';
      subscribe(sub,task,success,failure,done,loc);subReady=true;
      state.stage='returned_unconfirmed';
    }
  }catch(e){state.error=['registry_bounds','registry_cycle','duplicate_service','services_not_found','source_vtable','source_fields','image_source_fields','task_vtable','task_sources','send_guard'].includes(e.message)?e.message:'native_operation_error';state.stage='error';}
  finally{
    try {
    if(subReady)destroySubscription(sub);
    if(taskReady){const callable=task.add(32).readPointer();if(!callable.isNull()){
      if(!callable.readPointer().equals(base.add(0xa924968)))state.cleanup_error=true;
      else new NativeFunction(callable.readPointer().add(callable.equals(task)?32:40).readPointer(),'void',['pointer'])(callable);
      task.add(32).writePointer(ptr(0));
    }}
    if(pair)releasePair(pair);
    } catch (_) {state.cleanup_error=true;state.error='cleanup_failed';state.stage='error';}
  }
}
const glib=Process.enumerateModules().find(m=>m.name.startsWith('libglib-2.0.so'));
if(!glib)throw Error('native_schedule_failed');
const addIdle=new NativeFunction(glib.getExportByName('g_idle_add_full'),'uint',['int','pointer','pointer','pointer']);
const removeSource=new NativeFunction(glib.getExportByName('g_source_remove'),'int',['uint']);
const callback=new NativeCallback(()=>{sourceId=0;if(pending){pending=false;execute();}return 0;},'int',['pointer']);
const destroyed=new NativeCallback(()=>{if(pinned){pinned=false;Script.unpin();}},'void',['pointer']);
rpc.exports.status=()=>({...state,pinned});
rpc.exports.arm=()=>{
  if(state.stage!=='observing'||state.cancelled||sourceId||pinned||state.calls)return {accepted:false};
  pending=true;state.stage='armed';Script.pin();pinned=true;
  try{sourceId=addIdle(200,callback,ptr(0),destroyed);}catch(_){pending=false;pinned=false;Script.unpin();throw Error('native_schedule_failed');}
  if(!sourceId){pending=false;if(pinned){pinned=false;Script.unpin();}}
  return {accepted:sourceId>0};
};
rpc.exports.cancel=()=>{
  state.cancelled=true;pending=false;
  if(sourceId){removeSource(sourceId);sourceId=0;}
  return {cancelled:true,calls:state.calls,stage:state.stage,pinned};
};
