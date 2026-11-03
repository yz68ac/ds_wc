import test from "node:test";
import assert from "node:assert/strict";
import { effectScope } from "vue";
import { useConsoleSession } from "../src/composables/useConsoleSession.ts";

class Events {
 handlers: Record<string, Function> = {};
 closed = 0;
 onerror: Function | null = null;
 addEventListener(name: string, handler: Function) { this.handlers[name] = handler; }
 close() { this.closed++; }
 emit(name: string, data: any = {}) { this.handlers[name]?.({data:JSON.stringify(data)}); }
}

test("expired session closes SSE and rejects events from the old connection", async () => {
 const scope=effectScope(), events=new Events();
 const session=scope.run(() => useConsoleSession({request: (async (path: string) => path==="/session" ? {authenticated:true,csrf:"synthetic"} : {instances:[]}) as any,eventSource:()=>events as any}))!;
 try {
  await session.initialize();
  events.emit("snapshot",{instances:[{id:"one"}]});
  assert.equal(session.connected.value,true);
  events.emit("expired");
  assert.equal(session.authenticated.value,false);
  assert.equal(session.snapshot.value,null);
  assert.equal(events.closed,1);
  events.emit("snapshot",{instances:[{id:"stale"}]});
  assert.equal(session.snapshot.value,null);
 } finally {scope.stop();}
});

test("unmount ignores an outstanding overview response and never opens SSE", async () => {
 const scope=effectScope(), events=new Events();
 let resolve: (value: any) => void = () => {};
 const pending=new Promise(done=>{resolve=done;});
 const session=scope.run(() => useConsoleSession({request: (async (path: string) => path==="/session" ? {authenticated:true,csrf:"synthetic"} : pending) as any,eventSource:()=>events as any}))!;
 const initializing=session.initialize();
 await Promise.resolve(); await Promise.resolve();
 scope.stop(); resolve({instances:[{id:"late"}]});
 await initializing;
 assert.equal(session.snapshot.value,null);
 assert.equal(session.authenticated.value,false);
 assert.equal(session.connected.value,false);
});
