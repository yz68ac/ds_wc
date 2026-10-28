// Run the ACTUAL agent against synthetic memory/functions; never load Frida/WeChat.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const profile=JSON.parse(fs.readFileSync(path.join(root,'src/wechat_linux/native/image_profile.json'),'utf8'));
const source=fs.readFileSync(path.join(root,'src/wechat_linux/native/image_agent.js'),'utf8');
let scenarios=0;
function world(mode='success') {
 const bytes=new Map(),functions=new Map(),futureResults=new Map(),jobs=new Map();
 let next=0x10000,nextFunction=0x70000000,timer=null,cmSymbols=null,now=0,co=null,apiCalls=0;
 const number=v=>v instanceof P?v.n:typeof v==='string'?Number(v):Number(v);
 class U {constructor(v){this.n=BigInt(v instanceof U?v.n:v);}toNumber(){return Number(this.n);}equals(v){return this.n===new U(v).n;}compare(v){return this.n<new U(v).n?-1:this.n>new U(v).n?1:0;}}
 class P {
  constructor(n){this.n=number(n);}add(n){return new P(this.n+number(n));}sub(n){return new P(this.n-number(n));}
  equals(v){return this.n===number(v);}isNull(){return this.n===0;}toString(){return '0x'+this.n.toString(16);}
  readU8(){return bytes.get(this.n)||0;}writeU8(v){bytes.set(this.n,v&255);}
  readU32(){return Number(this.readU64().n&0xffffffffn);}readS32(){return this.readU32()|0;}writeU32(v){this.writeBytes(BigInt(v>>>0),4);}
  readU64(){let v=0n;for(let i=7;i>=0;i--)v=(v<<8n)+BigInt(this.add(i).readU8());return new U(v);}
  writeBytes(v,n){v=BigInt(v);for(let i=0;i<n;i++){this.add(i).writeU8(Number(v&255n));v>>=8n;}}
  writeU64(v){this.writeBytes(v instanceof U?v.n:BigInt(v),8);}readPointer(){return new P(this.readU64().toNumber());}
  writePointer(p){this.writeU64(BigInt(number(p)));}writeByteArray(a){a=new Uint8Array(a);for(let i=0;i<a.length;i++)this.add(i).writeU8(a[i]);}
  readByteArray(n){return Uint8Array.from({length:n},(_,i)=>this.add(i).readU8()).buffer;}
  readUtf8String(n){const a=[];for(let i=0;i<(n===undefined?4096:n);i++){const b=this.add(i).readU8();if(n===undefined&&!b)break;a.push(b);}return Buffer.from(a).toString('utf8');}
 }
 const ptr=v=>new P(v),alloc=n=>{const p=ptr(next);next+=Math.ceil(n/16)*16+16;return p;};
 const text=s=>{const b=Buffer.from(s+'\0');const p=alloc(b.length);p.writeByteArray(b);return p;};
 function string(out,s){const b=Buffer.from(s);if(b.length<=22){out.writeU8(b.length*2);out.add(1).writeByteArray(b);}else{out.writeU8(1);out.add(8).writeU64(b.length);out.add(16).writePointer(text(s));}}
 const base=ptr(0x100000000),kernel=alloc(0x400),account=alloc(0x80),registry=alloc(0x100);
 const manager=alloc(0x1000),database=alloc(0x200),loop=alloc(0x80);
 base.add(0xacf6710).writePointer(kernel);kernel.add(0x68).writePointer(account);account.add(0x28).writePointer(registry);
 kernel.add(0x290).writePointer(loop);kernel.add(0x298).writePointer(alloc(24));loop.add(0x10).writePointer(alloc(32));
 const managerKey='m'.repeat(32),dbKey='d'.repeat(32);
 base.add(profile.key_rva).writeByteArray(Buffer.from(managerKey+'\0'));base.add(profile.db_key_rva).writeByteArray(Buffer.from(dbKey+'\0'));
 const nodes=[alloc(0x40),alloc(0x40)];registry.add(0x10).writePointer(nodes[0]);registry.add(0x18).writeU64(2);
 for(let i=0;i<2;i++){nodes[i].writePointer(i===0?nodes[1]:ptr(0));string(nodes[i].add(0x10),i===0?managerKey:dbKey);nodes[i].add(0x28).writePointer(i===0?manager:database);nodes[i].add(0x30).writePointer(alloc(24));}
 manager.writePointer(base.add(0xa8c4568));database.writePointer(base.add(0xa8b5090));
 for(const [rva,hex] of Object.entries(profile.functions))base.add(rva).writeByteArray(Buffer.from(hex,'hex'));
 const ti=base.add(profile.callback_void_type_rva);ti.writePointer(base.add(profile.callback_void_vtable_rva));ti.add(8).writePointer(text('v'));
 const config={pid:123,mode:'coroutine-download',chat:'synthetic@chatroom',sender:'wxid_person',image_md5:'a'.repeat(32),server_id:'18446744073709551615',timeout_ms:5000};
 function native(rva,fn){functions.set(base.add(rva).n,fn);}
 function future(out){const p=alloc(0x100);out.writePointer(p);out.add(8).writePointer(alloc(24));return p;}
 native(0xa674ee0,alloc);native(0x453da40,()=>{});native(0x453db20,()=>{});native(0x4539690,()=>{});
 native(0x4540610,()=>{});native(0x450c140,()=>{});
 native(0x450e6a0,(out,input,n)=>{string(out,input.readUtf8String(n));return out;});
 native(0x605f720,(out,model)=>{out.writePointer(model.add(0x238).readPointer());out.add(8).writePointer(model.add(0x240).readPointer());return out;});
 native(0x662c280,()=>1);native(0x625eb10,()=>0);native(0x6263860,(out)=>{out.writePointer(alloc(16));out.add(8).writePointer(alloc(24));return out;});
 native(0x6051e10,out=>{future(out);return out;});native(0x605bb40,data=>data.add(0x18));
 native(0x674bb30,(context,box)=>{
  const task=box.readPointer(),f=task.add(0x58).readPointer(),record=alloc(0x280);
  record.add(0xc).writeU32(3);record.add(0xf8).writeU64(new U(config.server_id));string(record.add(0x18),config.chat);
  string(record.add(0x48),mode==='wrong-sender'?'wxid_wrong':config.sender);
  string(record.add(0x130),`<msg><img md5="${mode==='wrong-digest'?'b'.repeat(32):config.image_md5}" /></msg>`);
  record.add(0x278).writeU8(mode==='not-found'?0:1);futureResults.set(f.n,record);
  f.add(0x84).writeU32(mode==='query-pending'?0:mode==='query-exception'?2:1);box.writePointer(ptr(0));return 1;
 });
 native(0x6541270,pair=>futureResults.get(pair.readPointer().n));
 native(0x6545070,pair=>futureResults.get(pair.readPointer().n));
 native(0x49770c0,pair=>{const job=jobs.get(pair.readPointer().n);if(job&&!job.released){job.released=true;job.obs.add(12).writeU32(job.obs.add(12).readU32()-1);}});
 function completeCoroutine({changed=false,refs=false,failure=false}={}){
  assert(co);const input=cmSymbols.invocation;co.obs.add(4).writeU32(999);co.obs.add(8).writeU32(1);co.obs.writeU32(1);
  input.add(104).writeU32(changed?3:2);
  if(!changed){apiCalls++;const df=future(input.add(24)),result=alloc(48);result.writeU32(0);result.add(4).writeU32(failure?5:0);df.add(0x84).writeU32(mode==='download-pending'?0:1);futureResults.set(df.n,result);}
  co.promise.add(0x84).writeU32(1);if(refs)co.obs.add(12).writeU32(co.obs.add(12).readU32()+1);
 }
 native(0x6247520,(out,loop,loc,fn)=>{
  const object=alloc(0x160),promise=alloc(0x100),obs=fn.add(8).readPointer();object.writePointer(base.add(0xaab5910));object.add(0xa0).writePointer(promise);
  out.writePointer(object);out.add(8).writePointer(alloc(24));co={object,promise,obs,released:false};jobs.set(object.n,co);
  obs.add(12).writeU32(obs.add(12).readU32()+1);
  if(mode!=='delayed'&&mode!=='account-changed')completeCoroutine({failure:mode==='download-failed'});
  return out;
 });
 const exportFunction=fn=>{const p=ptr(nextFunction++);functions.set(p.n,fn);return p;};
 const glibExports={g_timeout_add_full:exportFunction((priority,ms,cb,data,destroy)=>{timer={cb,destroy,active:true};return 1;}),g_atomic_int_add:exportFunction(()=>0)};
 const libcExports={pthread_self:exportFunction(()=>ptr(0)),gettid:exportFunction(()=>999)};
 const module=(name,exports)=>({name,getExportByName:key=>exports[key]});
 const glib=module('libglib-2.0.so.0',glibExports),libc=module('libc.so.6',libcExports);
 const context={PROFILE:profile,CONFIG:config,C_SOURCE:'synthetic-adapter',rpc:{exports:{}},Script:{pin(){},unpin(){}},
  Process:{id:123,arch:'x64',pointerSize:8,mainModule:{base,path:'/opt/wechat/wechat'},getCurrentThreadId:()=>123,enumerateModules:()=>[glib,libc],getModuleByName:()=>libc},
  Memory:{alloc,allocUtf8String:text},ptr,uint64:v=>new U(v),Date:{now:()=>now},
  NativeFunction:function(address){assert(functions.has(address.n),'missing native '+address);return functions.get(address.n);},
  NativeCallback:function(fn){return fn;},CModule:function(source,symbols){cmSymbols=symbols;return {callback_vtable:alloc(72),destroy_inline:exportFunction(fn=>{const obs=fn.add(8).readPointer();obs.add(12).writeU32(obs.add(12).readU32()-1);})};}};
 if(mode==='profile-mismatch')base.add(Object.keys(profile.functions)[0]).writeU8(0);
 vm.createContext(context);vm.runInContext(source,context,{timeout:1000});
 const api=context.rpc.exports;
 function tick(ms=50){now+=ms;assert(timer&&timer.active);const again=timer.cb(ptr(0));if(!again){timer.active=false;timer.destroy(ptr(0));}return api.status();}
 function finish(){for(let i=0;i<100&&timer?.active;i++)tick();assert(!timer?.active,JSON.stringify(api.status()));return api.status();}
 return {api,tick,finish,completeCoroutine,calls:()=>apiCalls,dropReference:()=>co.obs.add(12).writeU32(co.obs.add(12).readU32()-1)};
}
function test(name,body){body();scenarios++;}
test('cold-cache native completion',()=>{const w=world();w.api.arm();const s=w.finish();assert.equal(s.stage,'done');assert.equal(s.cleanup,true);assert.equal(s.native_inflight,false);assert.equal(w.calls(),1);});
test('cancel before native call',()=>{const w=world();w.api.arm();w.api.cancel();const s=w.finish();assert.equal(s.stage,'cancelled');assert.equal(w.calls(),0);assert.equal(s.pinned,false);});
test('cancel while coroutine is owned',()=>{const w=world('delayed');w.api.arm();w.tick();w.api.cancel();let s=w.tick();assert.equal(s.pinned,true);assert.equal(s.native_inflight,true);assert.equal(s.cleanup,false);w.completeCoroutine();s=w.finish();assert.equal(s.pinned,false);});
test('owned clone outlives coroutine',()=>{const w=world('delayed');w.api.arm();w.tick();w.completeCoroutine({refs:true});let s=w.tick();assert.equal(s.pinned,true);assert.equal(s.callback_references,1);w.dropReference();s=w.finish();assert.equal(s.callback_references,0);});
for(const mode of ['wrong-sender','wrong-digest','not-found','query-exception'])test(mode,()=>{const w=world(mode);w.api.arm();const s=w.finish();assert.equal(s.stage,'error');assert.equal(s.pinned,false);assert.equal(w.calls(),0);});
test('expired query never launches download',()=>{const w=world('query-pending');w.api.arm();w.tick(6000);const s=w.finish();assert.equal(s.error,'query_timeout');assert.equal(w.calls(),0);});
test('account changed in native callback',()=>{const w=world('account-changed');w.api.arm();w.tick();w.completeCoroutine({changed:true});const s=w.finish();assert.equal(s.error,'account_changed');assert.equal(w.calls(),0);assert.equal(s.pinned,false);});
test('pending download is uncertain at deadline',()=>{const w=world('download-pending');w.api.arm();w.tick();w.tick(6000);const s=w.finish();assert.equal(s.unconfirmed,true);assert.equal(s.callback_references,0);assert.equal(s.pinned,false);});
test('profile mismatch before arm',()=>{assert.throws(()=>world('profile-mismatch'),/profile_mismatch/);});
console.log(JSON.stringify({scenarios,live_process_access:false}));
