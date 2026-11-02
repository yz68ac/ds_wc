<script setup lang="ts">
import { onBeforeUnmount,onMounted,ref,watch } from 'vue';
import { ChatDotRound,Refresh,Search } from '@element-plus/icons-vue';
import { api } from '../api';
defineProps<{modelValue:string}>();
const emit=defineEmits<{'update:modelValue':[value:string];selected:[contact:any]}>();
const query=ref(''),kind=ref('all'),items=ref<any[]>([]),busy=ref(false),error=ref(''),manual=ref(false),total=ref(0),truncated=ref(false);
const selected=ref<any>(null);
let timer:ReturnType<typeof setTimeout>|undefined,controller:AbortController|undefined,generation=0;
async function load(refresh=false) {
  const current=++generation;controller?.abort();controller=new AbortController();busy.value=true;error.value='';
  try {
    const result=await api(`/contacts?q=${encodeURIComponent(query.value)}&kind=${kind.value}&refresh=${refresh}`,{signal:controller.signal});
    if(current!==generation)return;
    items.value=result.items;total.value=result.total;truncated.value=result.truncated;
  } catch(e:any) { if(current===generation && e.name!=='AbortError'){items.value=[];error.value=e.message;} }
  finally {if(current===generation)busy.value=false;}
}
watch([query,kind],()=>{clearTimeout(timer);generation++;controller?.abort();timer=setTimeout(()=>load(),300);});
onMounted(()=>load());
onBeforeUnmount(()=>{generation++;clearTimeout(timer);controller?.abort();});
function choose(item:any) {if(item.instance_id)return;selected.value=item;emit('update:modelValue',item.id);emit('selected',item);}
</script>

<template>
  <div class="contact-picker">
    <div class="contact-toolbar"><el-input v-model="query" :prefix-icon="Search" placeholder="搜索备注、昵称、群名或微信 ID" clearable aria-label="搜索联系人或群聊" /><el-button :icon="Refresh" :loading="busy" @click="load(true)" aria-label="刷新联系人" /></div>
    <el-radio-group v-model="kind" size="small" class="contact-filter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="group">群聊</el-radio-button><el-radio-button value="private">联系人</el-radio-button></el-radio-group>
    <el-alert v-if="error" :title="error" type="warning" :closable="false" show-icon />
    <div v-loading="busy" class="contact-results" role="listbox" aria-label="可选微信会话" :aria-busy="busy">
      <button v-for="item in items" :key="item.id" type="button" role="option" :aria-selected="modelValue===item.id" :disabled="!!item.instance_id" :class="['contact-choice',{selected:modelValue===item.id}]" @click="choose(item)">
        <span class="contact-icon"><el-icon><ChatDotRound /></el-icon></span><span class="contact-text"><b>{{ item.name }}</b><small>{{ item.kind==='group' ? '群聊' : '联系人' }}<template v-if="item.nickname && item.nickname!==item.name"> · {{ item.nickname }}</template><code>{{ item.id }}</code></small></span><span class="contact-assigned" v-if="item.instance_id">已绑定 {{ item.instance_id }}</span><span class="contact-check" v-else-if="modelValue===item.id">已选择</span>
      </button>
      <div v-if="!items.length && !busy" class="contact-empty">{{ error ? '可稍后刷新，或使用下方的手动输入。' : '没有找到可用会话，请换个关键词；也可在微信中打开该会话后刷新。' }}</div>
    </div>
    <small class="contact-note">{{ total }} 个匹配{{ truncated ? '，当前显示前 100 个，请继续搜索缩小范围' : '' }}。仅展示当前绑定账号已同步、发送后端支持的本地联系人和群聊。</small>
    <el-tag v-if="selected && selected.id===modelValue" type="success" class="contact-selection">已选择：{{ selected.name }}</el-tag>
    <el-checkbox v-model="manual">手动输入微信 ID</el-checkbox>
    <el-input v-if="manual" :model-value="modelValue" @update:model-value="(value:any)=>emit('update:modelValue',String(value))" placeholder="wxid_… 或 …@chatroom" aria-label="手动会话 ID" />
  </div>
</template>

<style scoped>
.contact-picker{width:100%}.contact-toolbar{display:flex;gap:8px}.contact-toolbar .el-input{flex:1}.contact-filter{margin:12px 0}.contact-results{max-height:220px;min-height:80px;overflow:auto;border:1px solid var(--ui-border);border-radius:10px;background:var(--ui-surface)}.contact-choice{display:flex;align-items:center;gap:10px;width:100%;padding:12px;text-align:left;background:transparent;border-bottom:1px solid var(--ui-border)}.contact-choice:last-child{border-bottom:0}.contact-choice:hover:not(:disabled),.contact-choice.selected{background:var(--ui-accent-soft)}.contact-choice:disabled{cursor:default;opacity:.6}.contact-icon{display:grid;place-items:center;width:34px;height:34px;flex-shrink:0;background:var(--ui-surface-soft);color:var(--ui-accent);border-radius:9px;font-size:18px}.contact-text{flex:1;min-width:0}.contact-text b{font-size:13px;color:var(--ui-text);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.contact-text small{font-size:11px;color:var(--ui-muted);line-height:1.6}.contact-text code{display:block;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.contact-assigned,.contact-check{font-size:11px;max-width:100px;overflow-wrap:anywhere}.contact-check{color:var(--ui-accent)}.contact-assigned{color:var(--ui-muted)}.contact-note{display:block;font-size:11px;line-height:1.8;color:var(--ui-muted);margin-top:8px}.contact-empty{padding:22px 16px;text-align:center;font-size:12px;line-height:1.8;color:var(--ui-muted)}.contact-picker>.el-alert{margin:4px 0 12px}
</style>
