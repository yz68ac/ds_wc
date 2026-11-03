<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { ChatDotRound } from "@element-plus/icons-vue";
import { api } from "../api";
const props = defineProps<{instances: any[]}>();
const emit = defineEmits<{saved: []; error: [error: any]}>();
const instances = computed(() => props.instances);
const promptId = ref(""), promptData = ref<any>(null), promptText = ref("");
const promptBusy = ref(false), promptLoading = ref(false), compareOpen = ref(false);
const promptDirty = computed(() => !!promptData.value && promptText.value !== promptData.value.text);
const selectedPrompt = computed(() => instances.value.find(item => item.id === promptId.value));
function fail(error: any) { emit('error',error); }
const formatTime = (value: number) => value ? new Date(value*1000).toLocaleString("zh-CN", {hour12:false}) : "暂无记录";
async function discardPrompt() {
  if (!promptDirty.value) return true;
  try {
    await ElMessageBox.confirm(
      "提示词尚未保存，离开会丢弃本次编辑。",
      "保留编辑还是离开？",
      {
        confirmButtonText: "丢弃并离开",
        cancelButtonText: "继续编辑",
        type: "warning",
      },
    );
    return true;
  } catch {
    return false;
  }
}
async function selectPrompt(id: string) {
  if (promptLoading.value) return;
  if (id !== promptId.value && !(await discardPrompt())) return;
  promptLoading.value = true;
  try {
    const data = await api(`/instances/${encodeURIComponent(id)}/prompt`);
    promptId.value = id;
    promptData.value = data;
    promptText.value = data.text;
  } catch (e) {
    fail(e);
  } finally {
    promptLoading.value = false;
  }
}
async function savePrompt() {
  if (!promptData.value || promptBusy.value) return;
  promptBusy.value = true;
  try {
    const data = await api(
      `/instances/${encodeURIComponent(promptId.value)}/prompt`,
      {
        method: "PUT",
        body: JSON.stringify({
          text: promptText.value,
          revision: promptData.value.revision,
          config_revision: promptData.value.config_revision,
        }),
      },
    );
    promptData.value = data;
    promptText.value = data.text;
    ElMessage.success(
      data.activation === "restart_required"
        ? "已创建独立提示词，应用实例配置后生效。"
        : "提示词已保存，下一次新请求读取。",
    );
    emit('saved');
  } catch (e) {
    fail(e);
  } finally {
    promptBusy.value = false;
  }
}
async function restorePrompt(id: string) {
  try {
    await ElMessageBox.confirm(
      "当前版本会先备份，再恢复所选版本。",
      "恢复提示词",
      { confirmButtonText: "恢复", cancelButtonText: "取消", type: "warning" },
    );
    const data = await api(
      `/instances/${encodeURIComponent(promptId.value)}/prompt/restore/${id}`,
      {
        method: "POST",
        body: JSON.stringify({
          revision: promptData.value.revision,
          config_revision: promptData.value.config_revision,
        }),
      },
    );
    promptData.value = data;
    promptText.value = data.text;
    ElMessage.success("已恢复，下一次新请求读取。");
  } catch (e: any) {
    if (e !== "cancel" && e !== "close") fail(e);
  }
}
async function reload(id: string) {
  if (id === promptId.value && !promptDirty.value) await selectPrompt(id);
}
watch(instances, (items) => {
  if (!promptId.value && items.length) void selectPrompt(items[0].id);
}, {immediate:true});
defineExpose({canLeave: discardPrompt, reload, dirty: promptDirty});
</script>
<template>
<div class="prompt-layout">
          <aside class="panel prompt-picker">
            <div class="panel-heading"><h2>选择实例</h2></div>
            <button
              v-for="item in instances"
              :key="item.id"
              :class="{ active: promptId === item.id }"
              @click="selectPrompt(item.id)"
            >
              <span class="chat-icon"
                ><el-icon><ChatDotRound /></el-icon
              ></span>
              <div>
                <b>{{ item.id }}</b
                ><small>{{ item.kind === "private" ? "私聊" : "群聊" }}</small>
              </div>
            </button>
            <div v-if="!instances.length" class="empty-state">请先创建实例</div>
          </aside>
          <section v-loading="promptLoading" class="panel prompt-editor">
            <template v-if="promptData"
              ><div class="panel-heading">
                <div>
                  <h2>
                    {{ selectedPrompt?.id }}
                    <span class="state-pill">{{ promptData.file }}</span>
                  </h2>
                  <small>仅编辑所属实例的系统提示词</small>
                </div>
                <span :class="['save-state', { dirty: promptDirty }]">{{
                  promptDirty ? "有未保存的修改" : "已保存"
                }}</span>
              </div>
              <div class="editor-note">
                保存后供下一次新请求读取，已在生成的回复继续使用原来的提示词。
              </div>
              <el-input
                v-model="promptText"
                type="textarea"
                resize="none"
                :rows="17"
                class="prompt-textarea"
                aria-label="系统提示词正文"
              />
              <div class="editor-footer">
                <small
                  >{{ promptText.length }} 字符
                  <span>UTF-8 · 纯文本</span></small
                >
                <div>
                  <el-button
                    :disabled="!promptDirty"
                    @click="compareOpen = true"
                    >对比改动</el-button
                  ><el-button
                    type="primary"
                    :disabled="!promptDirty || !promptText.trim()"
                    :loading="promptBusy"
                    @click="savePrompt"
                    >保存提示词</el-button
                  >
                </div>
              </div>
              <div class="history-section">
                <h3>历史版本 <span>每次保存前自动备份</span></h3>
                <div v-if="!promptData.history.length" class="history-empty">
                  首次保存后将在这里显示历史版本。
                </div>
                <div
                  v-for="entry in promptData.history.slice(0, 5)"
                  :key="entry.id"
                  class="history-row"
                >
                  <span>{{ formatTime(entry.created_at) }}</span
                  ><small>{{ entry.characters }} 字符</small
                  ><el-button
                    link
                    type="primary"
                    @click="restorePrompt(entry.id)"
                    >恢复此版本</el-button
                  >
                </div>
              </div></template
            >
            <div v-else class="empty-state large">
              选择一个实例，开始编辑提示词。
            </div>
          </section>
        </div>
<el-dialog v-model="compareOpen" title="提示词修改对比" width="900px"
    ><div class="diff-grid">
      <div>
        <h3>当前已保存</h3>
        <pre>{{ promptData?.text }}</pre>
      </div>
      <div>
        <h3>本次编辑</h3>
        <pre>{{ promptText }}</pre>
      </div>
    </div>
    <template #footer
      ><el-button @click="compareOpen = false">继续编辑</el-button></template
    ></el-dialog
  >
</template>
