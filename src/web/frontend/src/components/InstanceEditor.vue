<script setup lang="ts">
import { computed, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { prepareInstanceEditor } from "../editorModel";
import ContactPicker from "./ContactPicker.vue";
import SettingsFields from "./SettingsFields.vue";
const props = defineProps<{catalog: any; reloadModels: () => Promise<void>; catalogError: string; refreshModels: () => Promise<void>}>();
const emit = defineEmits<{saved: [id: string]; error: [error: any]}>();
function fail(error: any) { emit('error', error); }
const editorOpen = ref(false),
  editorBusy = ref(false),
  creating = ref(false),
  editorId = ref(""),
  editorRevision = ref("");
const editor = ref<any>({}),
  editorTab = ref("basic"),
  editorJson = ref("");
const settingsSchema=ref<any>(null), editorEffective=ref<any>({});
let previousEditorTab="basic";
const providerConnections = computed(() => props.catalog.providers.map((item: any) => item.id));
const selectedProvider = computed(() =>
  props.catalog.providers?.find(
    (provider: any) => provider.id === editor.value.model_clients?.reply,
  ),
);
const imageConnections = computed(() => props.catalog.providers.filter((p: any) => p.image_protocol));
const imageProvider = computed(() => props.catalog.providers.find((p: any) => p.id === editor.value.model_clients?.image));
const imageIsGemini = computed(() => imageProvider.value?.image_protocol === 'gemini');
function changeImageConnection() {
  const timeout = editor.value.models.image?.options?.timeout_seconds || 180;
  editor.value.models.image = {model: '', options: imageIsGemini.value
    ? {aspect_ratio: '1:1', timeout_seconds: timeout}
    : {size: '1024x1024', quality: 'low', timeout_seconds: timeout}};
}
const backgroundRoles = [
  { id: "participation", name: "群聊参与判断" },
  { id: "memory", name: "记忆整理" },
  { id: "avatar", name: "头像分类" },
  { id: "group_profile", name: "群资料分析" },
];
function modelOptions(connection: string) {
  return (
    props.catalog.providers?.find((item: any) => item.id === connection)
      ?.models || []
  );
}
function prepareEditor(config: any, effective: any = {}) {
 editor.value = prepareInstanceEditor(config, effective, settingsSchema.value?.defaults || {});
 editorJson.value = JSON.stringify(editor.value, null, 2);
}
async function editInstance(id?: string) {
  creating.value = !id;
  editorId.value = id || "chat-"+Array.from(crypto.getRandomValues(new Uint8Array(4)),v=>v.toString(16).padStart(2,"0")).join("");
  editorRevision.value = "";
  try {
    settingsSchema.value=await api("/config/schema");
    await props.reloadModels();
    editorEffective.value={};
    if (id) {
      const item = await api(`/instances/${encodeURIComponent(id)}`);
      editorEffective.value=item.effective;
      prepareEditor(item.config, item.effective);
      editorRevision.value = item.revision;
    } else
      prepareEditor({
        enabled: false,
        chat: "",
        system_prompt_file: "system.md",
        memory: {enabled:false,avatar:{enabled:false}},
        profile: {enabled:false},participation: {enabled:false},
      });
    editorTab.value="basic"; previousEditorTab="basic";
    editorOpen.value = true;
  } catch (e) {
    fail(e);
  }
}
function changeEditorTab(name: string | number) {
  const next=String(name);
  if(next==='json') editorJson.value=JSON.stringify(editor.value,null,2);
  else if(previousEditorTab==='json') {
    try { prepareEditor(JSON.parse(editorJson.value), editorEffective.value); }
    catch { editorTab.value='json'; ElMessage.error('JSON 格式有误，请先修正。'); return; }
  }
  previousEditorTab=next;
}
async function saveInstance(apply = false) {
  editorBusy.value = true;
  try {
    const config =
      editorTab.value === "json" ? JSON.parse(editorJson.value) : editor.value;
    const path = creating.value
      ? "/instances"
      : `/instances/${encodeURIComponent(editorId.value)}`;
    const saved = await api(path, {
      method: creating.value ? "POST" : "PUT",
      body: JSON.stringify(
        creating.value
          ? { id: editorId.value, config }
          : { config, revision: editorRevision.value },
      ),
    });
    editorRevision.value = saved.revision;
    // The instance now exists: if applying fails, a retry must update it instead of creating it again.
    creating.value = false;
    if (apply) {
      const result = await api(
        `/instances/${encodeURIComponent(editorId.value)}/actions/apply`,
        {
          method: "POST",
          body: JSON.stringify({ revision: saved.revision }),
        },
      );
      ElMessage.success(result.message);
    }
    editorOpen.value = false;
    if (!apply) ElMessage.success("实例配置已保存，可点击‘应用此实例’生效。");
    emit('saved', editorId.value);
  } catch (e: any) {
    if (e instanceof SyntaxError) ElMessage.error("JSON 格式有误，请先修正。");
    else fail(e);
  } finally {
    editorBusy.value = false;
  }
}
defineExpose({open: editInstance});
</script>
<template>
<el-dialog
    v-model="editorOpen"
    :title="creating ? '新建会话实例' : '编辑实例 · ' + editorId"
    width="min(800px, calc(100vw - 24px))"
    top="5vh"
    class="instance-config-dialog"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    destroy-on-close
    ><div class="dialog-note">
      配置可以单独保存；“保存并应用”只重建当前实例。正在执行的请求会先结束，其他实例继续运行。
    </div>
    <el-tabs v-model="editorTab" @tab-change="changeEditorTab"
      ><el-tab-pane label="基本配置" name="basic"
        ><el-form label-position="top" class="instance-form"
          ><div class="form-grid">
            <el-form-item v-if="!creating" label="实例标识"
              ><el-input
                v-model="editorId"
                :disabled="!creating"
                placeholder="例如：friends" /></el-form-item
            ><el-form-item label="启用实例"
              ><el-switch
                v-model="editor.enabled"
                active-text="启用"
                inactive-text="关闭"
            /></el-form-item>
          </div>
          <el-form-item v-if="creating" label="选择联系人或群聊"><ContactPicker v-model="editor.chat" /></el-form-item>
          <el-collapse v-if="creating" class="contact-advanced"><el-collapse-item title="高级：修改自动生成的实例标识" name="id"><el-input v-model="editorId" aria-label="实例标识" /></el-collapse-item></el-collapse>
          <el-form-item v-else label="已绑定会话"><el-input v-model="editor.chat" disabled /><small class="field-hint">会话绑定后不直接更换；需要其他会话时新建实例。</small></el-form-item>
          <div class="form-divider">回复模型</div>
          <div class="form-grid">
            <el-form-item label="回复连接"
              ><el-select v-model="editor.model_clients.reply"
                ><el-option
                  v-for="v in providerConnections"
                  :key="v"
                  :label="v"
                  :value="v" /></el-select></el-form-item
            ><el-form-item label="回复模型"
              ><el-select
                v-model="editor.models.reply.model"
                filterable
                allow-create
                default-first-option
                placeholder="选择模型或输入实际模型 ID"
              >
                <el-option
                  v-for="model in modelOptions(editor.model_clients.reply)"
                  :key="model.id"
                  :label="model.name"
                  :value="model.id"
                /> </el-select
            ></el-form-item>
          </div>
          <div class="form-grid three">
            <el-form-item label="思考"
              ><el-select v-model="editor.models.reply.options.thinking"
                ><el-option label="关闭" value="disabled" /><el-option
                  label="开启"
                  value="enabled" /></el-select></el-form-item
            ><el-form-item label="思考档位"
              ><el-select
                v-model="editor.models.reply.options.reasoning_effort"
                clearable
                placeholder="使用默认"
                ><el-option
                  v-for="v in ['low', 'medium', 'high', 'max']"
                  :key="v"
                  :label="v"
                  :value="v" /></el-select></el-form-item
            ><el-form-item label="请求超时 / 秒"
              ><el-input-number
                v-model="editor.models.reply.options.timeout_seconds"
                :min="1"
                :max="300"
                controls-position="right"
            /></el-form-item>
          </div>
          <div class="form-grid">
            <el-form-item label="最大输出 token"
              ><el-input-number
                v-model="editor.models.reply.options.max_tokens"
                :min="1"
                :max="128000"
                controls-position="right" /></el-form-item
            ><el-form-item label="自动回复"
              ><el-switch
                v-model="editor.participation.enabled"
                active-text="启用"
            /></el-form-item>
          </div>
          <small class="field-hint"
            >自动回复开启后，私聊直接回复，群聊由参与模型决策；关闭后不再接收新的自动回复消息。列表来自当前连接的模型目录，也可手动输入模型
            ID。</small
          >
          <div class="catalog-toolbar">
            <small v-if="selectedProvider">{{
              !selectedProvider.configured
                ? "当前连接尚未配置 API 密钥"
                : selectedProvider.state === "stale"
                  ? "连接配置已变化，需重新拉取模型列表"
                  : selectedProvider.fetched_at
                    ? selectedProvider.models.length +
                      " 个模型 · 更新于 " +
                      new Date(selectedProvider.fetched_at).toLocaleString(
                        "zh-CN",
                      )
                    : "尚无模型目录，可拉取或直接输入模型 ID"
            }}</small>
            <el-button
              size="small"
              :loading="catalog.refreshing"
              @click="refreshModels"
              >从提供商刷新模型列表</el-button
            ><small>{{
              catalog.refreshing
                ? "正在拉取，旧列表仍可使用"
                : "仅在点击时访问已配置的模型提供商"
            }}</small>
          </div>
          <el-alert
            v-if="catalogError || catalog.error"
            :title="
              catalogError || '模型目录无效，可重新拉取或手动输入模型 ID。'
            "
            type="warning"
            :closable="false"
          />
          <el-alert
            v-if="
              catalog.last_refresh &&
              Object.keys(catalog.last_refresh.errors || {}).length
            "
            :title="
              '部分连接刷新失败：' +
              Object.entries(catalog.last_refresh.errors)
                .map(([key, value]) => key + ': ' + value)
                .join('；')
            "
            type="warning"
            :closable="false"
          />
          <el-collapse class="background-settings">
            <el-collapse-item title="图片生成" name="image">
              <el-form-item label="允许生图与改图"><el-switch v-model="editor.image_generation.enabled" active-text="启用" /></el-form-item>
              <div class="form-grid">
                <el-form-item label="图片连接"><el-select v-model="editor.model_clients.image" @change="changeImageConnection">
                  <el-option v-for="p in imageConnections" :key="p.id" :label="p.id" :value="p.id" />
                </el-select></el-form-item>
                <el-form-item label="图片模型"><el-select v-model="editor.models.image.model" filterable allow-create default-first-option placeholder="选择或输入实际生图模型 ID">
                  <el-option v-for="model in modelOptions(editor.model_clients.image)" :key="model.id" :label="model.name" :value="model.id" />
                </el-select></el-form-item>
              </div>
              <div class="form-grid" v-if="imageIsGemini">
                <el-form-item label="画面比例"><el-select v-model="editor.models.image.options.aspect_ratio" placeholder="默认 1:1">
                  <el-option v-for="v in ['1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9']" :key="v" :value="v" :label="v" />
                </el-select></el-form-item>
                <el-form-item label="分辨率"><el-select v-model="editor.models.image.options.image_size" clearable @clear="delete editor.models.image.options.image_size" placeholder="模型默认">
                  <el-option v-for="v in ['512','1K','2K','4K']" :key="v" :value="v" :label="v" />
                </el-select></el-form-item>
              </div>
              <div class="form-grid" v-else>
                <el-form-item label="尺寸"><el-select v-model="editor.models.image.options.size" placeholder="默认 1024x1024">
                  <el-option v-for="v in ['1024x1024','1024x1536','1536x1024','auto']" :key="v" :value="v" :label="v" />
                </el-select></el-form-item>
                <el-form-item label="质量"><el-select v-model="editor.models.image.options.quality" placeholder="默认 low">
                  <el-option v-for="v in ['low','medium','high','auto']" :key="v" :value="v" :label="v" />
                </el-select></el-form-item>
              </div>
              <el-form-item label="生图超时 / 秒"><el-input-number v-model="editor.models.image.options.timeout_seconds" :min="1" :max="300" controls-position="right" /></el-form-item>
              <small class="field-hint">用户明确要求绘图或引用图片要求修改时，由回复模型调用。改图只使用本轮成功读取的引用图片，每次一张；任务独立排队，完成后发回当前会话。请为改图选择支持图片编辑的图片模型，并开启图片理解。</small>
              <el-alert v-if="editor.image_generation.enabled && !imageProvider?.image_configured" title="图片连接尚未配齐：请在 provider .env 中填写密钥；Gemini 还需要 GEMINI_IMAGE_BASE_URL。" type="warning" :closable="false" />
            </el-collapse-item>
          </el-collapse>
          <el-collapse class="background-settings"
            ><el-collapse-item title="后台模型与任务" name="background">
              <el-form-item label="后台共用连接"
                ><el-select v-model="editor.model_clients.background"
                  ><el-option
                    v-for="value in providerConnections"
                    :key="value"
                    :label="value"
                    :value="value" /></el-select
              ></el-form-item>
              <div class="background-toggles">
                <el-switch
                  v-model="editor.memory.enabled"
                  active-text="记忆"
                /><el-switch
                  v-model="editor.memory.avatar.enabled"
                  active-text="头像分类"
                  :disabled="!editor.memory.enabled"
                /><el-switch
                  v-model="editor.profile.enabled"
                  active-text="群资料分析"
                />
              </div>
              <div
                v-for="role in backgroundRoles"
                :key="role.id"
                class="background-role"
              >
                <el-form-item :label="role.name"
                  ><el-select
                    v-model="editor.models[role.id].model"
                    filterable
                    allow-create
                    default-first-option
                    placeholder="选择模型或输入模型 ID"
                    ><el-option
                      v-for="model in modelOptions(
                        editor.model_clients.background,
                      )"
                      :key="model.id"
                      :label="model.name"
                      :value="model.id" /></el-select
                ></el-form-item>
                <div class="form-grid">
                  <el-form-item label="思考"
                    ><el-select
                      v-model="editor.models[role.id].options.thinking"
                      ><el-option label="关闭" value="disabled" /><el-option
                        label="开启"
                        value="enabled" /></el-select></el-form-item
                  ><el-form-item label="思考档位"
                    ><el-select
                      v-model="editor.models[role.id].options.reasoning_effort"
                      clearable
                      placeholder="默认"
                      ><el-option
                        v-for="value in ['low', 'medium', 'high', 'max']"
                        :key="value"
                        :label="value"
                        :value="value" /></el-select
                  ></el-form-item>
                </div>
              </div> </el-collapse-item
          ></el-collapse> </el-form></el-tab-pane
      ><el-tab-pane label="业务参数" name="settings"><SettingsFields v-if="settingsSchema" v-model="editor" :effective="editorEffective" :groups="settingsSchema.groups" /></el-tab-pane
      ><el-tab-pane label="模型参数" name="model-options"><SettingsFields v-if="settingsSchema" v-model="editor" :effective="editorEffective" :groups="settingsSchema.model_groups" /></el-tab-pane
      ><el-tab-pane label="高级 JSON" name="json"
        ><el-input
          v-model="editorJson"
          type="textarea"
          :rows="20"
          class="json-editor"
          spellcheck="false"
          aria-label="实例配置 JSON"
        /><small class="field-hint"
          >仅接受实例配置；不接受 API 密钥、账号数据库或运行目录覆盖。</small
        ></el-tab-pane
      ></el-tabs
    ><template #footer
      ><el-button @click="editorOpen = false">取消</el-button
      ><el-button :loading="editorBusy" @click="saveInstance(false)"
        >仅保存</el-button
      ><el-button
        type="primary"
        :loading="editorBusy"
        @click="saveInstance(true)"
        >保存并应用此实例</el-button
      ></template
    ></el-dialog
  >
</template>
