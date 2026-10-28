// Version-locked, source-bound image download. No UI, sends or user-supplied code.
// Lifecycle: query -> verify -> native coroutine -> native future -> owned cleanup.
const base=Process.mainModule.base;
if(Process.arch!=='x64'||Process.pointerSize!==8||Process.id!==CONFIG.pid||Process.mainModule.path!=='/opt/wechat/wechat')throw Error('wrong_client');
for(const [offset,bytes] of Object.entries(PROFILE.functions)){
 const got=Array.from(new Uint8Array(base.add(offset).readByteArray(bytes.length/2))).map(v=>v.toString(16).padStart(2,'0')).join('');
 if(got!==bytes)throw Error('profile_mismatch');
}
if(CONFIG.mode!=='coroutine-download'||PROFILE.backend!=='image_coroutine_v1')throw Error('profile_mismatch');
if(!Number.isInteger(CONFIG.timeout_ms)||CONFIG.timeout_ms<5000||CONFIG.timeout_ms>30000)throw Error('invalid_timeout');
const voidType=base.add(PROFILE.callback_void_type_rva);
if(!voidType.readPointer().equals(base.add(PROFILE.callback_void_vtable_rva))||voidType.add(8).readPointer().readUtf8String()!=='v')throw Error('profile_mismatch');
if(!/^[A-Za-z0-9_-]+@chatroom$/.test(CONFIG.chat)||! /^[1-9][0-9]{0,19}$/.test(CONFIG.server_id)||! /^[a-f0-9]{32}$/.test(CONFIG.image_md5))throw Error('invalid_binding');
if((CONFIG.server_id.length===20&&CONFIG.server_id>'18446744073709551615')||! /^[A-Za-z0-9_-]{1,128}$/.test(CONFIG.sender))throw Error('invalid_binding');
const state={stage:'ready',queries:0,downloads:0,direct_wait_calls:0,cleanup:false};
const alloc=new NativeFunction(base.add(0xa674ee0),'pointer',['ulong']);
const taskCtor=new NativeFunction(base.add(0x453da40),'void',['pointer','pointer','pointer']);
const taskRelease=new NativeFunction(base.add(0x453db20),'void',['pointer']);
const createFuture=new NativeFunction(base.add(0x6051e10),'pointer',['pointer']);
const copyCore=new NativeFunction(base.add(0x605f720),'pointer',['pointer','pointer']);
const chatKind=new NativeFunction(base.add(0x662c280),'int',['pointer']);
const dbCount=new NativeFunction(base.add(0x625eb10),'int',['pointer','int']);
const dbAt=new NativeFunction(base.add(0x6263860),'pointer',['pointer','pointer','int','uint']);
const schedule=new NativeFunction(base.add(0x674bb30),'int',['pointer','pointer']);
const location=new NativeFunction(base.add(0x4540610),'void',['pointer','pointer','pointer','int','pointer']);
const destroyString=new NativeFunction(base.add(0x4539690),'void',['pointer']);
const chatOf=new NativeFunction(base.add(0x605bb40),'pointer',['pointer']);
const typedResult=new NativeFunction(base.add(0x6541270),'pointer',['pointer']);
const release=new NativeFunction(base.add(0x49770c0),'void',['pointer']);
const makeString=new NativeFunction(base.add(0x450e6a0),'void',['pointer','pointer','ulong']);
let sourceId=0,pinned=false,cancelled=false,future=null,chat=null,coroTask=null,coroLoop=null,invocation=null,coSettled=0,expiresAt=0,accountAnchor=null;
const observation=Memory.alloc(16);observation.writeByteArray(new Uint8Array(16));
invocation=Memory.alloc(128);invocation.writeByteArray(new Uint8Array(128));
const libc=Process.getModuleByName('libc.so.6');
const nativeGlib=Process.enumerateModules().find(m=>m.name.startsWith('libglib-2.0.so'));
const cm=new CModule(C_SOURCE,{client_alloc:base.add(0xa674ee0),client_free:base.add(0x450c140),ref_add:nativeGlib.getExportByName('g_atomic_int_add'),thread_self:libc.getExportByName('pthread_self'),thread_id:libc.getExportByName('gettid'),invocation,void_type:voidType,start_download:base.add(0x6517dc0)});
const destroyInline=new NativeFunction(cm.destroy_inline,'void',['pointer']);
const spawnCoroutine=new NativeFunction(base.add(0x6247520),'pointer',['pointer','pointer','pointer','pointer','int']);
const typedDownload=new NativeFunction(base.add(0x6545070),'pointer',['pointer']);
function zero(n){const p=Memory.alloc(n);p.writeByteArray(new Uint8Array(n));return p;}
function string(p,max=128){const f=p.readU8(),n=(f&1)?p.add(8).readU64().toNumber():f>>>1;if(!Number.isSafeInteger(n)||n<0||n>max)throw Error('invalid_string');return (f&1)?p.add(16).readPointer().readUtf8String(n):p.add(1).readUtf8String(n);}
function service(){
 const kernel=base.add(0xacf6710).readPointer(),account=kernel.add(0x68).readPointer(),core=account.add(0x28).readPointer();
 const count=core.add(0x18).readU64().toNumber();if(count<1||count>512)throw Error('registry_bounds');
 const keys={manager:base.add(PROFILE.key_rva).readUtf8String(32),database:base.add(PROFILE.db_key_rva).readUtf8String(32)};
 let node=core.add(0x10).readPointer(),seen=new Set(),found={};
 for(let i=0;!node.isNull()&&i<count;i++){
  if(seen.has(node.toString()))throw Error('registry_cycle');seen.add(node.toString());
  for(const name of Object.keys(keys))if(string(node.add(0x10))===keys[name]){
   const obj=node.add(0x28).readPointer(),vt=name==='manager'?0xa8c4568:0xa8b5090;
   if(found[name]||node.add(0x30).readPointer().isNull()||!obj.readPointer().equals(base.add(vt)))throw Error('service_type');found[name]=obj;
  }
  node=node.readPointer();
 }
 if(!found.manager||!found.database)throw Error('service_not_unique');
 accountAnchor=account;state.service_verified=true;return found;
}
function copyPair(out,object,control){const model=zero(0x248);model.add(0x238).writePointer(object);model.add(0x240).writePointer(control);copyCore(out,model);}
let svc=null,kind=0,index=0,last=0,deadline=0;
function beginQuery(){
 const db=zero(16),context=zero(32),box=zero(8);let task=null;
 try{
  state.stage='select_database';dbAt(db,svc.database,kind,index);if(db.readPointer().isNull()||db.add(8).readPointer().isNull())throw Error('database_missing');
  future=zero(16);createFuture(future);
  task=alloc(0x68);taskCtor(task,base.add(0x6baa5a0),base.add(0x6baa750));
  task.add(0x28).writeByteArray(new Uint8Array(0x40));box.writePointer(task);
  makeString(task.add(0x28),Memory.allocUtf8String(CONFIG.chat),CONFIG.chat.length);
  task.add(0x40).writeU64(uint64(CONFIG.server_id));
  task.add(0x48).writePointer(db.readPointer());task.add(0x50).writePointer(db.add(8).readPointer());db.writePointer(ptr(0));db.add(8).writePointer(ptr(0));
  copyPair(task.add(0x58),future.readPointer(),future.add(8).readPointer());
  location(context,base.add(0xd9729a),base.add(0xe65922),0x6a1,base.add(0x6baa5a0));
  state.stage='query_scheduling';state.queries++;state.schedule_result=schedule(context,box);state.stage='waiting';
 }finally{taskRelease(box);release(db);}
}
function verify(data){
 state.record_type=data.add(0xc).readU32();state.server_matches=data.add(0xf8).readU64().equals(uint64(CONFIG.server_id));
 state.chat_matches=string(chatOf(data))===CONFIG.chat;
 state.sender_matches=string(data.add(0x48))===CONFIG.sender;
 const xml=string(data.add(0x130),256*1024);
 const md5=/<img\b[^>]*\bmd5\s*=\s*["']([0-9a-f]{32})["']/i.exec(xml);
 state.digest_matches=md5!==null&&md5[1].toLowerCase()===CONFIG.image_md5;
 if(state.record_type!==3||!state.server_matches||!state.chat_matches||!state.sender_matches||!state.digest_matches)throw Error('message_binding_mismatch');
 state.message_verified=true;
}

function cleanup(){
 if(observation.add(12).readU32()!==0||coroTask)throw Error('native_callback_still_owned');
 if(!invocation.add(32).readPointer().isNull()){release(invocation.add(24));invocation.add(24).writePointer(ptr(0));invocation.add(32).writePointer(ptr(0));}
 if(!invocation.add(16).readPointer().isNull()){release(invocation.add(8));invocation.add(8).writePointer(ptr(0));invocation.add(16).writePointer(ptr(0));}
 if(coroLoop){release(coroLoop);coroLoop=null;}
 if(future){release(future);future=null;}
 if(chat){destroyString(chat);chat=null;}state.cleanup=true;
}
function launchCoroutine(data){
 if(cancelled||Date.now()>=expiresAt)throw Error('query_timeout');
 if(!base.add(0xacf6710).readPointer().add(0x68).readPointer().equals(accountAnchor))throw Error('account_changed');
 invocation.writePointer(svc.manager);copyPair(invocation.add(8),data,future.add(8).readPointer());invocation.add(40).writeU32(1);
 const kernel=base.add(0xacf6710).readPointer();invocation.add(112).writePointer(kernel);invocation.add(120).writePointer(accountAnchor);coroLoop=zero(16);copyPair(coroLoop,kernel.add(0x290).readPointer(),kernel.add(0x298).readPointer());
 if(coroLoop.readPointer().isNull()||coroLoop.readPointer().add(0x10).readPointer().isNull())throw Error('loop_missing');
 const fn=zero(40);observation.add(12).writeU32(1);fn.writePointer(cm.callback_vtable);fn.add(8).writePointer(observation);fn.add(32).writePointer(fn);
 const loc=zero(32);location(loc,base.add(0xd9729a),base.add(0xe65922),0x6a1,base.add(0x6baa5a0));coroTask=zero(16);
 state.stage='coroutine_spawning';state.downloads=1;state.resource_kind=2;state.request_origin=200;
 try{spawnCoroutine(coroTask,coroLoop.readPointer(),loc,fn,0);}finally{destroyInline(fn);}
 if(coroTask.readPointer().isNull()||!coroTask.readPointer().readPointer().equals(base.add(0xaab5910)))throw Error('coroutine_task_type');
 state.stage='coroutine_waiting';deadline=expiresAt;
}
function pollCoroutine(){
 if(coroTask){
  const promise=coroTask.readPointer().add(0xa0).readPointer();state.coroutine_completion_state=promise.add(0x84).readS32();
  if(state.coroutine_completion_state===0)return 1;
  state.coroutine_context_available=observation.add(8).readU32()===1;state.worker_is_main=observation.add(4).readU32()===Process.id;state.download_call_returned=invocation.add(104).readU32()===2;
  release(coroTask);coroTask=null;release(coroLoop);coroLoop=null;
 }
 state.callback_references=observation.add(12).readU32();if(state.callback_references!==0)return 1;
 if(++coSettled<3)return 1;
 if(invocation.add(104).readU32()===3)throw Error('account_changed');
 if(state.error)throw Error(state.error);
 if(state.coroutine_completion_state!==1||!state.download_call_returned)throw Error('coroutine_failed');
 const pair=invocation.add(24);if(pair.readPointer().isNull()||pair.add(8).readPointer().isNull())throw Error('download_future_missing');
 state.download_future_state=pair.readPointer().add(0x84).readS32();state.stage='download_waiting';
 if(state.download_future_state===0){if(Date.now()>deadline||cancelled){state.unconfirmed=true;cleanup();state.stage='done';sourceId=0;return 0;}return 1;}
 if(state.download_future_state!==1)throw Error('download_future_exception');
 const result=typedDownload(pair);state.download_status=[result.readS32(),result.add(4).readS32()];cleanup();state.stage='done';sourceId=0;return 0;
}

function tick(){
 if(Process.getCurrentThreadId()!==Process.id){state.error='wrong_thread';if(coroTask||observation.add(12).readU32())return 1;state.stage='wrong_thread';return 0;}
 try{
  if(['coroutine_waiting','download_waiting','native_cleanup_wait'].includes(state.stage))return pollCoroutine();
  if(cancelled){cleanup();state.stage='cancelled';sourceId=0;return 0;}
  if(state.stage==='armed'){
   svc=service();chat=zero(24);makeString(chat,Memory.allocUtf8String(CONFIG.chat),CONFIG.chat.length);
   if(string(chat)!==CONFIG.chat)throw Error('string_roundtrip');
   kind=chatKind(chat);last=dbCount(svc.database,kind);if(last<0||last>8)throw Error('database_bounds');
   state.database_count=last+1;state.database_kind=kind;deadline=expiresAt;beginQuery();
  }
  if(future.readPointer().isNull()||future.add(8).readPointer().isNull())throw Error('future_missing');
  const value=future.readPointer().add(0x84).readS32();state.future_state=value;
  if(value===0){if(Date.now()>deadline)throw Error('query_timeout');state.stage='waiting';return 1;}
  if(value!==1)throw Error('future_exception');
  const data=typedResult(future);if(data.isNull())throw Error('result_missing');
  state.optional_present=data.add(0x278).readU8();
  if(state.optional_present===0){release(future);future=null;if(++index<=last){beginQuery();return 1;}throw Error('message_not_found');}
  if(state.optional_present!==1)throw Error('invalid_optional');verify(data);
  launchCoroutine(data);return 1;
 }catch(e){
  state.failure_stage=state.stage;
  if(['access-violation','illegal-instruction','abort'].includes(e.type))state.native_exception_type=e.type;
  state.error=['registry_bounds','registry_cycle','service_type','service_not_unique','lookup_type','string_roundtrip','future_missing','future_exception','result_missing','invalid_query_returned_message','unexpected_long_string','account_changed','loop_missing','coroutine_task_type','coroutine_failed','download_future_missing','download_future_exception','database_missing','database_bounds','query_timeout','message_not_found','invalid_optional','message_binding_mismatch'].includes(e.message)?e.message:'native_query_error';
  if(coroTask||observation.add(12).readU32()!==0){state.stage='native_cleanup_wait';return 1;}
  try{cleanup();}catch(_){state.cleanup_error=true;}
  state.stage='error';sourceId=0;return 0;
 }
}
const glib=Process.enumerateModules().find(m=>m.name.startsWith('libglib-2.0.so'));
if(!glib)throw Error('glib_missing');
const add=new NativeFunction(glib.getExportByName('g_timeout_add_full'),'uint',['int','uint','pointer','pointer','pointer']);
const callback=new NativeCallback(tick,'int',['pointer']);
const destroyed=new NativeCallback(()=>{if(pinned){pinned=false;Script.unpin();}},'void',['pointer']);
rpc.exports.arm=()=>{
 if(state.stage!=='ready'||pinned||state.queries)return {accepted:false};
 expiresAt=Date.now()+CONFIG.timeout_ms;state.stage='armed';Script.pin();pinned=true;sourceId=add(200,50,callback,ptr(0),destroyed);
 if(!sourceId){pinned=false;Script.unpin();state.stage='schedule_failed';}return {accepted:sourceId>0};
};
rpc.exports.status=()=>({...state,pinned,source_pending:sourceId>0,callback_references:observation.add(12).readU32(),native_inflight:coroTask!==null||observation.add(12).readU32()!==0});
rpc.exports.cancel=()=>{cancelled=true;return {requested:true};};
