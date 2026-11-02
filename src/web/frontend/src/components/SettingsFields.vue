<script setup lang="ts">
import { computed, ref, watch } from 'vue';
const props=withDefaults(defineProps<{modelValue:any; effective?:any; groups:any[]; disabled?:boolean; searchable?:boolean; resettable?:boolean}>(),{searchable:true,resettable:true});
const emit=defineEmits<{ 'update:modelValue':[value:any] }>();
const query=ref('');
function read(value:any,path:string) { return path.split('.').reduce((v,k)=>v?.[k],value); }
function inherited(field:any) {
  if(Object.hasOwn(field,'default')) return field.default;
  const value=read(props.effective,field.path);
  return value===undefined ? field.default : value;
}
function value(field:any) {
  const configured=read(props.modelValue,field.path);
  return configured===undefined ? inherited(field) : configured;
}
function set(field:any,value:any,clear=false) {
  // Configuration documents contain JSON values, including nested Vue proxies.
  const next=JSON.parse(JSON.stringify(props.modelValue)), parts=field.path.split('.');
  let target=next;
  for (const name of parts.slice(0,-1)) target=target[name] ||= {};
  if(clear || value===undefined || value===null || field.optional && value==='') delete target[parts.at(-1)!];
  else target[parts.at(-1)!]=value;
  emit('update:modelValue',next);
}
const filtered=computed(()=>props.groups.map(group=>({...group,fields:group.fields.filter((f:any)=>
  `${group.label} ${f.label} ${f.path}`.toLowerCase().includes(query.value.trim().toLowerCase()))})).filter(group=>group.fields.length));
const expanded=ref<string[]>(props.groups.length ? [props.groups[0].id] : []);
watch(query,()=>{if(query.value.trim())expanded.value=filtered.value.map(group=>group.id);});
</script>

<template>
  <div class="settings-fields">
    <el-input v-if="searchable!==false" v-model="query" placeholder="搜索设置，例如：上下文、重试、头像" clearable aria-label="搜索配置项" class="settings-search" />
    <el-collapse v-model="expanded">
      <el-collapse-item v-for="group in filtered" :key="group.id" :name="group.id">
        <template #title><span class="settings-group-title">{{ group.label }}</span><span class="settings-count">{{ group.fields.length }} 项</span></template>
        <p class="settings-description">{{ group.description }}</p>
        <div class="settings-grid">
          <div v-for="field in group.fields" :key="field.path" class="setting-field" :class="{ overridden: resettable!==false && read(modelValue,field.path)!==undefined }">
            <div class="setting-label"><label :for="`setting-${field.path}`">{{ field.label }}</label><el-button v-if="resettable!==false && read(modelValue,field.path)!==undefined && !disabled" link size="small" @click="set(field,undefined,true)">恢复默认</el-button></div>
            <el-switch v-if="field.type==='boolean'" :id="`setting-${field.path}`" :model-value="value(field)" :disabled="disabled" active-text="开启" inactive-text="关闭" @update:model-value="(v:any)=>set(field,v)" />
            <el-input-number v-else-if="field.type==='number'" :id="`setting-${field.path}`" :model-value="value(field) ?? undefined" :placeholder="field.optional ? '模型默认' : undefined" :disabled="disabled" :min="field.min" :max="field.max ?? undefined" :step="field.step ?? (field.integer ? 1 : 0.1)" :precision="field.integer ? 0 : undefined" controls-position="right" :aria-label="field.label" @update:model-value="(v:any)=>set(field,v)" />
            <el-select v-else-if="field.type==='select'" :id="`setting-${field.path}`" :model-value="value(field)" :disabled="disabled" :clearable="field.optional" :aria-label="field.label" @update:model-value="(v:any)=>set(field,v)"><el-option v-for="choice in field.choices" :key="choice" :value="choice" :label="choice" /></el-select>
            <el-select v-else-if="field.type==='list'" :id="`setting-${field.path}`" :model-value="value(field)" :disabled="disabled" multiple filterable allow-create default-first-option :aria-label="field.label" placeholder="输入一项后按回车添加" @update:model-value="(v:any)=>set(field,v)"><el-option v-for="item in value(field) || []" :key="item" :label="item" :value="item" /></el-select>
            <el-input v-else :id="`setting-${field.path}`" :model-value="value(field) ?? ''" :disabled="disabled" :clearable="field.optional" :aria-label="field.label" :placeholder="field.optional ? '使用模型默认值' : ''" @update:model-value="(v:any)=>set(field,v)" />
            <small>{{ resettable===false ? '账号共享' : read(modelValue,field.path)===undefined ? '默认值' : '自定义' }}<code>{{ field.path }}</code></small>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>
    <div v-if="!filtered.length" class="settings-empty">没有匹配的配置项</div>
  </div>
</template>

<style scoped>
.settings-fields { min-width: 0; }
.settings-fields :deep(.el-collapse) { max-height: 54vh; overflow: auto; scrollbar-gutter: stable; border-top: 0; }
.settings-fields :deep(.el-collapse-item__header) { height: 50px; padding: 0 14px; margin-bottom: 10px; border: 1px solid var(--ui-border); border-radius: 10px; background: var(--ui-surface-soft); color: var(--ui-text); }
.settings-fields :deep(.el-collapse-item__header.is-active) { color: var(--ui-accent); border-color: #cfe3da; background: #f4faf6; }
.settings-fields :deep(.el-collapse-item__wrap) { border: 0; }
.settings-fields :deep(.el-collapse-item__content) { padding: 6px 4px 20px; }
.settings-search { margin: 8px 0 18px; }
.settings-group-title { font-size: 14px; font-weight: 600; }
.settings-count { font-size: 11px; font-weight: 500; color: var(--ui-muted); margin-left: 10px; padding: 1px 7px; border-radius: 5px; background: #eaf0ed; line-height: 1.8; }
.settings-description { color: var(--ui-muted); font-size: 12px; line-height: 1.85; margin: 0 0 20px; }
.settings-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 24px 28px; padding: 0 2px 8px; }
.setting-field { min-width: 0; }
.setting-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 26px; margin-bottom: 8px; }
.setting-label label { color: var(--ui-secondary); font-size: 13px; font-weight: 500; }
.setting-label .el-button { font-size: 11px; min-height: 0; padding: 0; opacity: .8; }
.setting-label .el-button:hover { opacity: 1; }
.setting-field .el-input-number,.setting-field .el-select { width: 100%; }
.setting-field small { display: flex; justify-content: space-between; gap: 8px; color: var(--ui-muted); font-size: 11px; line-height: 1.7; margin-top: 7px; min-height: 19px; }
.setting-field.overridden small { color: var(--ui-accent); }
.setting-field code { font-size: 10px; font-family: 'Cascadia Code',Consolas,monospace; color: var(--ui-muted); opacity: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: opacity .15s; }
.setting-field:hover code,.setting-field:focus-within code { opacity: 1; }
.settings-empty { padding: 30px; text-align: center; color: var(--ui-muted); }
@media(max-width:600px) { .settings-grid { grid-template-columns: 1fr; gap: 18px; } .setting-field code { display: none; } }
</style>
