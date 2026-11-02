<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api';
import SettingsFields from './SettingsFields.vue';
const emit=defineEmits<{refresh:[];error:[error:any]}>();
const data=ref<any>(null),groups=ref<any[]>([]),form=ref<any>({}),initial=ref('');
const busy=ref(false),error=ref(''),saved=ref(false);
const dirty=computed(()=>JSON.stringify(form.value)!==initial.value);
async function load() {
  busy.value=true;error.value='';
  try {
    const [settings,schema]=await Promise.all([api('/account/settings'),api('/config/schema')]);
    const next:any={};
    for(const [path,value] of Object.entries(settings.values)) {
      const parts=path.split('.');let target=next;
      for(const part of parts.slice(0,-1)) target=target[part] ||= {};
      target[parts.at(-1)!]=value;
    }
    data.value=settings;form.value=next;initial.value=JSON.stringify(next);
    groups.value=[{id:'account',label:'账号业务参数',description:'影响此账号下的所有实例。保存后需在服务管理应用全部配置；保存不会自动启动机器人。',fields:schema.account_fields}];
  } catch(e:any) { error.value=e.message;if(e.status===401)emit('error',e); }
  finally { busy.value=false; }
}
async function reload() {
  if(dirty.value) { try { await ElMessageBox.confirm('重新读取会丢弃尚未保存的账号参数。','重新读取',{confirmButtonText:'丢弃并读取',cancelButtonText:'继续编辑'}); } catch{return;} }
  await load();
}
async function save() {
  try { await ElMessageBox.confirm('修改会影响所有实例。保存后仍需应用全部配置，正在运行的机器人不会由此自动重启。','保存账号参数',{confirmButtonText:'保存',cancelButtonText:'取消'}); } catch{return;}
  busy.value=true;error.value='';
  try {
    const values:any={};
    for(const field of groups.value[0].fields) {
      const value=field.path.split('.').reduce((x:any,k:string)=>x?.[k],form.value);
      if(value===undefined || value===null) throw new Error(`${field.label}不能为空`);
      values[field.path]=value;
    }
    const response=await api('/account/settings',{method:'PUT',body:JSON.stringify({values,revision:data.value.revision})});
    data.value=response;initial.value=JSON.stringify(form.value);saved.value=true;
    ElMessage.success('账号参数已保存，请在服务管理应用全部配置。');emit('refresh');
  } catch(e:any) { error.value=e.message;if(e.status===401)emit('error',e); }
  finally {busy.value=false;}
}
onMounted(load);
async function canLeave() {
  if(!dirty.value) return true;
  try { await ElMessageBox.confirm('账号参数尚未保存，离开会丢弃本次修改。','离开设置',{confirmButtonText:'丢弃并离开',cancelButtonText:'继续编辑'});return true; }
  catch {return false;}
}
defineExpose({dirty,canLeave});
</script>

<template>
  <section v-loading="busy" class="panel account-settings">
    <div class="panel-heading"><div><h2>账号设置</h2><small>此账号共享的轮询、发送与确认参数</small></div><el-button size="small" @click="reload">重新读取</el-button></div>
    <div class="account-settings-body">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <SettingsFields v-if="data" v-model="form" :effective="form" :groups="groups" :searchable="false" :resettable="false" :disabled="busy || !data.editable" />
      <el-alert v-if="data && !data.editable" title="当前账号配置目录只读，请在服务器上修改。" type="info" :closable="false" />
      <p v-if="data" class="runtime-note">当前读取方式：{{ data.runtime.source }} · 发送后端：{{ data.runtime.backend }} · Provider 配置：{{ data.runtime.provider_env || '默认路径' }}。账号绑定、数据库和内存目录由运行环境管理。</p>
      <div class="account-settings-footer"><small>{{ dirty ? '有未保存的修改' : saved ? '已保存，应用全部配置后生效' : '配置已读取' }}</small><el-button type="primary" :loading="busy" :disabled="!dirty || !data?.editable" @click="save">保存账号参数</el-button></div>
    </div>
  </section>
</template>

<style scoped>
.account-settings-body { padding: 0 24px 20px; }
.account-settings-body>.el-alert { margin-bottom: 16px; }
.account-settings-footer { display: flex; justify-content: space-between; align-items: center; gap: 15px; margin-top: 18px; }
.account-settings-footer small,.runtime-note { font-size: 12px; line-height: 1.8; color: var(--ui-muted); }
.runtime-note { margin-top: 18px; padding: 12px 14px; border: 1px solid var(--ui-border); border-radius: 9px; background: var(--ui-surface-soft); overflow-wrap: anywhere; }
@media(max-width:600px) { .account-settings-body { padding: 0 16px 18px; } }
</style>
