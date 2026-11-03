import test from "node:test";
import assert from "node:assert/strict";
import { isProxy, reactive, ref } from "vue";
import { prepareInstanceEditor } from "../src/editorModel.ts";

test("configuration precedence preserves nulls, custom fields and future model roles", () => {
 const defaults = {models:{reply:{model:"default",options:{temperature:0.7,timeout_seconds:60}},future:{model:"future",options:{}}}, memory:{enabled:true,avatar:{enabled:true}},model_clients:{reply:"official"}};
 const effective = {models:{reply:{model:"effective",options:{timeout_seconds:30}}}};
 const raw = {models:{reply:{options:{temperature:null}}},custom:{keep:[1,2]}};
 const before = structuredClone({defaults,effective,raw});
 const value = prepareInstanceEditor(raw,effective,defaults);
 assert.equal(value.models.reply.model,"effective");
 assert.equal(value.models.reply.options.temperature,null);
 assert.equal(value.models.reply.options.timeout_seconds,30);
 assert.equal(value.models.future.model,"future");
 assert.equal(value.memory.avatar.enabled,true);
 value.custom.keep.push(3);
 value.models.reply.options.timeout_seconds=1;
 assert.deepEqual({defaults,effective,raw},before);
});

test("basic form does not materialize advanced defaults and explicit false stays false", () => {
 const value = prepareInstanceEditor({memory:{enabled:false},search:{enabled:false}}, {}, {memory:{enabled:true},vision:{enabled:true},target:{context_max_chars:100}});
 assert.equal(value.memory.enabled,false);
 assert.equal(value.search.enabled,false);
 assert.equal(value.vision,undefined);
 assert.equal(value.target,undefined);
});

test("JSON keys cannot modify object prototypes during merge", () => {
 const value = prepareInstanceEditor(JSON.parse('{"models":{"__proto__":{"polluted":true}}}'));
 assert.equal(({} as any).polluted,undefined);
 assert.equal(Object.getPrototypeOf(value.models),null);
 assert.equal(value.models.__proto__.polluted,true);
});

test("reactive instance and schema values are detached even with nested proxies", () => {
 const names = reactive(["小助手"]);
 const defaults = ref({participation:{address_names:names},models:{reply:{options:{timeout_seconds:60}}}});
 const effective = ref({models:{reply:{model:"synthetic"}}});
 const flags = reactive(["first"]);
 const config = reactive({custom:{items:reactive([{flags}])},models:{reply:{options:{temperature:null}}}});
 const value = prepareInstanceEditor(config,effective.value,defaults.value);
 assert.equal(value.models.reply.model,"synthetic");
 assert.equal(value.models.reply.options.timeout_seconds,60);
 assert.equal(value.models.reply.options.temperature,null);
 assert.deepEqual(value.participation.address_names,["小助手"]);
 assert.equal(isProxy(value.participation.address_names),false);
 assert.equal(isProxy(value.custom.items[0].flags),false);
 value.participation.address_names.push("第二个");
 value.custom.items[0].flags.push("second");
 assert.deepEqual([...names],["小助手"]);
 assert.deepEqual([...flags],["first"]);
});
