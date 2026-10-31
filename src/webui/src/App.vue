<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  ChatDotRound,
  DataBoard,
  EditPen,
  Setting,
  Plus,
  Refresh,
  ArrowRight,
  SwitchButton,
  Connection,
  CircleCheck,
  Clock,
} from "@element-plus/icons-vue";
import { api, setCsrf } from "./api";

const componentLabel: Record<string, string> = { bot: "机器人", wechat: "微信会话", sender: "发送组件", recovery: "自动恢复", web: "管理台" };

function contextSummary(value: any) {
  if (!value) return "暂无请求";
  if (value.error) return value.error === "context_required_too_large" ? "当前消息或引用超过预算" : "上下文组装失败";
  return `${value.chars} 字符 · 历史 ${value.retained}/${value.selected} 条 · 裁剪 ${value.omitted} 条 · 扫描 ${value.scanned} 条${value.scan_limit_hit ? "（达到扫描上限）" : ""}`;
}

const pages = [
  { id: "overview", name: "工作台", subtitle: "OVERVIEW", icon: DataBoard },
  {
    id: "instances",
    name: "会话实例",
    subtitle: "INSTANCES",
    icon: ChatDotRound,
  },
  { id: "prompts", name: "系统提示词", subtitle: "PROMPTS", icon: EditPen },
  { id: "services", name: "服务管理", subtitle: "SERVICES", icon: Setting },
];
const page = ref("overview"),
  authenticated = ref(false),
  initialized = ref(false),
  demo = ref(false);
const password = ref(""),
  loginBusy = ref(false),
  loginError = ref(""),
  error = ref("");
const snapshot = ref<any>(null),
  connected = ref(false),
  loading = ref(false);
const promptId = ref(""),
  promptData = ref<any>(null),
  promptText = ref(""),
  promptBusy = ref(false),
  promptLoading = ref(false);
const compareOpen = ref(false),
  filter = ref(""),
  statusFilter = ref("all");
const editorOpen = ref(false),
  editorBusy = ref(false),
  creating = ref(false),
  editorId = ref(""),
  editorRevision = ref("");
const editor = ref<any>({}),
  editorTab = ref("basic"),
  editorJson = ref("");
const catalog = ref<any>({ providers: [], refreshing: false });
const providerConnections = computed(() =>
  catalog.value.providers.map((provider: any) => provider.id),
);
const catalogError = ref("");
const selectedProvider = computed(() =>
  catalog.value.providers?.find(
    (provider: any) => provider.id === editor.value.model_clients?.reply,
  ),
);
const imageConnections = computed(() => catalog.value.providers.filter((p: any) => p.image_protocol));
const imageProvider = computed(() => catalog.value.providers.find((p: any) => p.id === editor.value.model_clients?.image));
const imageIsGemini = computed(() => imageProvider.value?.image_protocol === 'gemini');
function changeImageConnection() {
  const timeout = editor.value.models.image.options.timeout_seconds || 180;
  editor.value.models.image = {model: '', options: imageIsGemini.value
    ? {aspect_ratio: '1:1', timeout_seconds: timeout}
    : {size: '1024x1024', quality: 'low', timeout_seconds: timeout}};
}
const instanceBusy = ref("");
const instanceRequests = ref<Record<string, string>>({});
const statusId = ref("");
const statusOpen = ref(false);
const statusInstance = computed(() =>
  instances.value.find((item) => item.id === statusId.value),
);
const backgroundRoles = [
  { id: "participation", name: "群聊参与判断" },
  { id: "memory", name: "记忆整理" },
  { id: "avatar", name: "头像分类" },
  { id: "group_profile", name: "群资料分析" },
];
const instanceLabels: Record<string, string> = {
  running: "运行中",
  stopping: "正在停止",
  stopped: "已停止",
  waiting_service: "等待账号服务",
  error: "启动/停止失败",
  unknown: "状态未知",
};
const shouldStopInstance = (item: any) =>
  item.enabled || ["running", "stopping"].includes(item.runtime_state);
const instanceErrors: Record<string, string> = {
  instance_target_not_allowed: "会话尚未加入原生发送授权清单。",
  instance_configuration_changed: "配置在操作期间发生变化，请重新应用。",
  account_config_changed_restart_required:
    "账号配置发生变化，需要重启账号服务。",
  instance_chat_identity_mismatch: "会话身份核对失败。",
  instance_transition_failed:
    "实例切换失败，请检查账号登录、模型配置和运行事件。",
};
let source: EventSource | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
const title = computed(() => pages.find((p) => p.id === page.value)!);
const instances = computed<any[]>(() => snapshot.value?.instances || []);
const enabledCount = computed(
  () => instances.value.filter((i) => i.enabled).length,
);
const running = computed(
  () => snapshot.value?.services?.["bot"]?.state === "active",
);
const promptDirty = computed(
  () => !!promptData.value && promptText.value !== promptData.value.text,
);
const selectedPrompt = computed(() =>
  instances.value.find((i) => i.id === promptId.value),
);
const filtered = computed(() =>
  instances.value.filter(
    (i) =>
      `${i.id} ${i.chat} ${i.reply_model}`
        .toLowerCase()
        .includes(filter.value.toLowerCase()) &&
      (statusFilter.value === "all" ||
        i.enabled === (statusFilter.value === "enabled")),
  ),
);
const pendingOperation = computed(() =>
  snapshot.value?.operations?.some((op: any) => op.state === "running"),
);
const canControl = computed(
  () =>
    snapshot.value?.capabilities?.service_control && !pendingOperation.value,
);
const stateLabel: Record<string, string> = {
  listening: "正在监听",
  idle: "空闲",
  stopped: "已停止",
  stale: "心跳已过期",
  unavailable: "状态不可用",
};
const serviceLabel: Record<string, string> = {
  active: "运行中",
  inactive: "已停止",
  failed: "启动失败",
  activating: "正在启动",
  deactivating: "正在停止",
  unavailable: "不可用",
};
const loginLabel: Record<string, string> = {
  logged_in: "已登录",
  waiting: "等待界面",
  login_ready: "等待登录",
  phone_confirmation: "待手机确认",
  open_ready: "等待进入微信",
  opening_wechat: "正在进入微信",
  qr_expired: "二维码已过期",
  waiting_manual: "等待手动处理",
  client_stopped: "微信已停止",
  completed: "登录流程已结束",
  confirmation_pending: "待手机确认",
  qr_required: "待扫码",
  ready: "可登录",
  unknown: "状态未知",
};
const formatTime = (value: number | undefined) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "暂无记录";

function fail(e: any) {
  if (e?.status === 401) {
    authenticated.value = false;
    source?.close();
    connected.value = false;
  }
  ElMessage.error(e?.message || "操作失败");
}
async function refresh() {
  loading.value = true;
  try {
    snapshot.value = await api("/overview");
    for (const item of snapshot.value.instances || []) {
      if (
        instanceRequests.value[item.id] === item.request_id ||
        snapshot.value.services?.["bot"]?.state !== "active"
      )
        delete instanceRequests.value[item.id];
    }
    error.value = "";
  } catch (e: any) {
    error.value = e.message;
    if (e.status === 401) fail(e);
  } finally {
    loading.value = false;
  }
}
async function loadModels() {
  try {
    catalog.value = await api("/models");
    catalogError.value = "";
  } catch (e: any) {
    catalogError.value = e.message;
  }
}
function modelOptions(connection: string) {
  return (
    catalog.value.providers?.find((item: any) => item.id === connection)
      ?.models || []
  );
}
async function refreshModels() {
  try {
    const result = await api("/models/refresh", { method: "POST", body: "{}" });
    ElMessage.success(result.message || "已开始从配置的提供商拉取模型列表。");
    await loadModels();
  } catch (e) {
    fail(e);
  }
}
async function instanceAction(item: any, kind: "start" | "stop" | "apply") {
  const names = { start: "启用实例", stop: "停用实例", apply: "应用此实例" };
  try {
    await ElMessageBox.confirm(
      kind === "stop"
        ? `停用 ${item.id}：停止新任务，等待已在执行的请求结束。其他实例继续运行。`
        : `${names[kind]} ${item.id}。若账号服务已停止，会保留配置等待下次启动，不会自动启动账号服务。`,
      names[kind],
      {
        confirmButtonText: "确认",
        cancelButtonText: "取消",
        type: kind === "stop" ? "warning" : "info",
      },
    );
    instanceBusy.value = item.id;
    const result = await api(
      `/instances/${encodeURIComponent(item.id)}/actions/${kind}`,
      {
        method: "POST",
        body: JSON.stringify({ revision: item.revision }),
      },
    );
    if (result.state === "requested")
      instanceRequests.value[item.id] = result.request_id;
    ElMessage.success(result.message);
    await refresh();
  } catch (e: any) {
    if (e !== "cancel" && e !== "close") fail(e);
  } finally {
    instanceBusy.value = "";
  }
}
function showStatus(id: string) {
  statusId.value = id;
  statusOpen.value = true;
}
function subscribe() {
  source?.close();
  source = new EventSource("/api/events");
  source.addEventListener("snapshot", (event: MessageEvent) => {
    snapshot.value = JSON.parse(event.data);
    for (const item of snapshot.value.instances || []) {
      if (
        instanceRequests.value[item.id] === item.request_id ||
        snapshot.value.services?.["bot"]?.state !== "active"
      )
        delete instanceRequests.value[item.id];
    }
    connected.value = true;
    error.value = "";
  });
  source.addEventListener("expired", () => {
    authenticated.value = false;
    source?.close();
    connected.value = false;
  });
  source.onerror = () => {
    connected.value = false;
  };
}
async function enter() {
  await refresh();
  await loadModels();
  subscribe();
}
async function login() {
  loginBusy.value = true;
  loginError.value = "";
  try {
    const result = await api("/login", {
      method: "POST",
      body: JSON.stringify({ password: password.value }),
    });
    authenticated.value = true;
    demo.value = result.demo;
    setCsrf(result.csrf);
    password.value = "";
    await enter();
  } catch (e: any) {
    loginError.value = e.message;
  } finally {
    loginBusy.value = false;
  }
}
async function logout() {
  if (!(await discardPrompt())) return;
  try {
    await api("/session", { method: "DELETE" });
    source?.close();
    authenticated.value = false;
    connected.value = false;
    snapshot.value = null;
  } catch (e) {
    fail(e);
  }
}
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
async function navigate(id: string) {
  if (id !== page.value && !(await discardPrompt())) return;
  if (id !== page.value && promptDirty.value)
    promptText.value = promptData.value.text;
  page.value = id;
  if (id === "prompts" && instances.value.length && !promptId.value)
    await selectPrompt(instances.value[0].id);
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
    await refresh();
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
function prepareEditor(config: any, effective: any = {}) {
  const value = structuredClone(config);
  value.model_clients = {
    reply: "anthropic",
    image: "openai",
    background: "deepseek",
    ...value.model_clients,
  };
  value.models ||= {};
  value.image_generation = {enabled: false, ...effective.image_generation, ...value.image_generation};
  value.models.image = {
    model: '', ...effective.models?.image, ...value.models.image,
    options: {timeout_seconds: 180, ...effective.models?.image?.options, ...value.models.image?.options},
  };
  value.models.reply = {
    model: "",
    ...effective.models?.reply,
    ...value.models.reply,
    options: {
      max_tokens: 1200,
      timeout_seconds: 60,
      thinking: "disabled",
      ...effective.models?.reply?.options,
      ...value.models.reply?.options,
    },
  };
  value.participation = { enabled: false, ...value.participation };
  for (const role of backgroundRoles) {
    value.models[role.id] = {
      model: "",
      ...effective.models?.[role.id],
      ...value.models[role.id],
      options: {
        max_tokens: 1200,
        timeout_seconds: 60,
        thinking: "disabled",
        ...effective.models?.[role.id]?.options,
        ...value.models[role.id]?.options,
      },
    };
  }
  value.memory = {
    enabled: false,
    ...value.memory,
    avatar: { enabled: false, ...value.memory?.avatar },
  };
  value.profile = { enabled: false, ...value.profile };
  editor.value = value;
  editorJson.value = JSON.stringify(value, null, 2);
  editorTab.value = "basic";
}
async function editInstance(id?: string) {
  creating.value = !id;
  editorId.value = id || "";
  editorRevision.value = "";
  try {
    await loadModels();
    if (id) {
      const item = await api(`/instances/${encodeURIComponent(id)}`);
      prepareEditor(item.config, item.effective);
      editorRevision.value = item.revision;
    } else
      prepareEditor({
        enabled: false,
        chat: "",
        system_prompt_file: "system.md",
      });
    editorOpen.value = true;
  } catch (e) {
    fail(e);
  }
}
function changeEditorTab(name: string | number) {
  if (name === "json") editorJson.value = JSON.stringify(editor.value, null, 2);
  else {
    try {
      prepareEditor(JSON.parse(editorJson.value));
    } catch {
      editorTab.value = "json";
      ElMessage.error("JSON 格式有误，请先修正。");
    }
  }
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
    await refresh();
    if (promptId.value === editorId.value && !promptDirty.value)
      await selectPrompt(promptId.value);
  } catch (e: any) {
    if (e instanceof SyntaxError) ElMessage.error("JSON 格式有误，请先修正。");
    else fail(e);
  } finally {
    editorBusy.value = false;
  }
}
async function action(kind: "start" | "stop" | "apply") {
  const copy = {
    start: ["启动机器人", "将加载已保存的配置，开始监听并恢复自动恢复。"],
    stop: [
      "停止机器人",
      "将停止机器人及自动恢复。微信登录会话保留，未完成队列由机器人关闭流程保存。",
    ],
    apply: [
      "应用全部配置",
      "所有运行实例会随账号服务受控重启。也可在实例页单独应用某个实例。账号服务已停止时，不会自动启动。",
    ],
  };
  try {
    await ElMessageBox.confirm(copy[kind][1], copy[kind][0], {
      confirmButtonText: "确认操作",
      cancelButtonText: "取消",
      type: kind === "stop" ? "warning" : "info",
    });
    await api(`/services/${kind}`, { method: "POST", body: "{}" });
    ElMessage.success("操作已提交，可在服务管理中查看结果。");
    await refresh();
  } catch (e: any) {
    if (e !== "cancel" && e !== "close") fail(e);
  }
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (promptDirty.value) {
    event.preventDefault();
    event.returnValue = "";
  }
}
onMounted(async () => {
  window.addEventListener("beforeunload", beforeUnload);
  try {
    const session = await api("/session");
    demo.value = session.demo;
    authenticated.value = session.authenticated;
    setCsrf(session.csrf || "");
    if (session.authenticated) await enter();
  } catch (e: any) {
    loginError.value = e.message;
  } finally {
    initialized.value = true;
  }
  timer = setInterval(() => {
    if (authenticated.value && !connected.value) refresh();
    if (authenticated.value && catalog.value.refreshing) loadModels();
  }, 5000);
});
onBeforeUnmount(() => {
  source?.close();
  if (timer) clearInterval(timer);
  window.removeEventListener("beforeunload", beforeUnload);
});
</script>

<template>
  <div v-if="!initialized" class="opening">
    WXBRIDGE <span>正在连接管理服务…</span>
  </div>
  <div v-else-if="!authenticated" class="login-layout">
    <div class="login-story">
      <div class="brand"><span class="brand-symbol">w</span> WXBRIDGE</div>
      <div class="login-heading">
        <span class="eyebrow">YOUR CONVERSATIONS, CONNECTED.</span>
        <h1>让每一次交流，<br />有条不紊。</h1>
        <p>
          一个安静、清晰的控制台。<br />照看会话实例，调整回应方式，掌握运行状态。
        </p>
        <div class="story-rule"></div>
        <small>微信机器人 · 本地管理平台</small>
      </div>
      <span class="story-decoration" aria-hidden="true">↗</span>
    </div>
    <main class="login-panel">
      <div class="login-card">
        <span class="eyebrow">CONSOLE ACCESS</span>
        <h2>欢迎回来</h2>
        <p>输入管理口令，进入你的工作台。</p>
        <el-alert
          v-if="demo"
          title="演示环境：仅使用合成数据，不连接真实微信。"
          type="info"
          :closable="false"
        />
        <form @submit.prevent="login">
          <label for="password">管理口令</label
          ><el-input
            id="password"
            v-model="password"
            type="password"
            show-password
            size="large"
            placeholder="请输入管理口令"
            autocomplete="current-password"
          />
          <div v-if="loginError" class="form-error" role="alert">
            {{ loginError }}
          </div>
          <el-button
            class="login-button"
            native-type="submit"
            type="primary"
            size="large"
            :loading="loginBusy"
            >进入管理台 <el-icon><ArrowRight /></el-icon
          ></el-button>
        </form>
        <small v-if="demo">演示口令：wxbridge-demo-local</small
        ><small v-else>口令与模型 API 密钥独立，由服务器管理员设置。</small>
      </div>
    </main>
  </div>
  <div v-else class="console-layout">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-symbol">w</span>
        <div>WXBRIDGE<small>微信机器人管理台</small></div>
      </div>
      <div class="nav-label">工作空间</div>
      <nav>
        <button
          v-for="item in pages"
          :key="item.id"
          :aria-label="item.name"
          :title="item.name"
          :class="{ selected: page === item.id }"
          @click="navigate(item.id)"
        >
          <el-icon><component :is="item.icon" /></el-icon
          ><span>{{ item.name }}</span
          ><span v-if="item.id === 'instances'" class="nav-count">{{
            instances.length
          }}</span>
        </button>
      </nav>
      <div class="sidebar-bottom">
        <div class="node-card">
          <span class="node-dot"></span>
          <div>
            本地节点<small>{{ demo ? "隔离演示环境" : "独立管理服务" }}</small>
          </div>
          <el-icon><Connection /></el-icon>
        </div>
        <div class="sidebar-footer">
          <span>CONSOLE <b>0.1</b></span
          ><button @click="logout" aria-label="退出登录">
            <el-icon><SwitchButton /></el-icon>
          </button>
        </div>
      </div>
    </aside>
    <div class="main-area">
      <header class="topbar">
        <div class="breadcrumb">
          工作空间 <span>/</span><b>{{ title.name }}</b>
        </div>
        <div class="topbar-right">
          <span v-if="demo" class="demo-label">演示环境</span
          ><span class="connection"
            ><i :class="{ live: connected }"></i
            >{{ connected ? "实时连接" : "正在重连" }}</span
          ><span class="avatar">管</span>
        </div>
      </header>
      <main class="workspace">
        <div v-if="demo" class="demo-notice">
          演示中的启停与编辑只影响合成数据，真实微信和机器人保持原状。
        </div>
        <el-alert
          v-if="error"
          :title="error"
          type="error"
          :closable="false"
          class="page-alert"
        />
        <el-alert
          v-if="snapshot?.runtime?.send_blocked"
          title="账号存在尚未确认的发送记录，已暂停新的发送。可查看相关实例状态，接收和模型任务仍可继续处理。"
          type="warning"
          :closable="false"
          class="page-alert"
        />
        <div class="page-heading">
          <div>
            <span class="eyebrow">{{ title.subtitle }}</span>
            <h1>
              {{ title.name
              }}<span v-if="page === 'instances'" class="heading-count">{{
                instances.length
              }}</span>
            </h1>
            <p>
              {{
                page === "overview"
                  ? "会话、模型与运行状态，尽在此处。"
                  : page === "instances"
                    ? "为每段会话配置独立的回应方式。"
                    : page === "prompts"
                      ? "让回复更像你希望的样子。"
                      : "管理机器人生命周期，保留微信登录会话。"
              }}
            </p>
          </div>
          <div class="heading-actions">
            <el-button :icon="Refresh" :loading="loading" @click="refresh"
              >刷新</el-button
            ><el-button
              v-if="page === 'instances'"
              type="primary"
              :icon="Plus"
              @click="editInstance()"
              >新建实例</el-button
            ><el-button
              v-if="page === 'overview'"
              type="primary"
              @click="navigate('instances')"
              >管理实例 <el-icon><ArrowRight /></el-icon
            ></el-button>
          </div>
        </div>

        <template v-if="page === 'overview'">
          <div class="metrics">
            <article class="metric-card">
              <div class="metric-top">
                机器人状态<el-icon><Connection /></el-icon>
              </div>
              <div class="metric-value status-value">
                <i :class="['status-dot', { live: running }]"></i
                >{{ stateLabel[snapshot?.runtime?.state] || "正在读取" }}
              </div>
              <small>{{
                running
                  ? "心跳 " + snapshot.runtime.heartbeat_age_seconds + " 秒前"
                  : "网页服务独立运行，可继续管理配置"
              }}</small>
            </article>
            <article class="metric-card">
              <div class="metric-top">
                会话实例<el-icon><ChatDotRound /></el-icon>
              </div>
              <div class="metric-value">
                {{ enabledCount }}<span>/ {{ instances.length }}</span>
              </div>
              <small>已启用 / 全部实例</small>
            </article>
            <article class="metric-card">
              <div class="metric-top">
                收发队列<el-icon><Clock /></el-icon>
              </div>
              <div class="metric-value">
                {{ snapshot?.runtime?.queue_length ?? "—" }}<span>项</span>
              </div>
              <small>{{
                snapshot?.runtime?.snapshot_current
                  ? "包含停用实例保留的任务"
                  : "最后快照 · 非实时队列"
              }}</small>
            </article>
            <article class="metric-card">
              <div class="metric-top">
                模型任务<el-icon><DataBoard /></el-icon>
              </div>
              <div class="metric-value">
                {{ snapshot?.runtime?.model_workers ?? "—"
                }}<span>{{
                  snapshot?.runtime?.snapshot_current ? "运行中" : "上次快照"
                }}</span>
              </div>
              <small>回复 · 图片 · 后台三条通道</small>
            </article>
          </div>
          <div v-if="snapshot?.runtime?.config_pending" class="pending-strip">
            <div>
              <el-icon><Clock /></el-icon><span>存在尚未应用的实例配置</span
              ><small>{{
                running
                  ? "保存的配置与当前运行版本不同"
                  : "下次启动时会加载已保存配置"
              }}</small>
            </div>
            <el-button
              size="small"
              :disabled="!canControl"
              @click="action('apply')"
              >重启应用全部配置</el-button
            >
          </div>
          <div class="overview-grid">
            <section class="panel">
              <div class="panel-heading">
                <div>
                  <h2>会话实例</h2>
                  <small>每个实例独立维护模型与队列</small>
                </div>
                <button class="text-link" @click="navigate('instances')">
                  查看全部 <el-icon><ArrowRight /></el-icon>
                </button>
              </div>
              <div v-if="!instances.length" class="empty-state">
                还没有实例，先添加一个群聊或私聊。
              </div>
              <button
                v-for="item in instances.slice(0, 5)"
                :key="item.id"
                class="instance-row"
                @click="editInstance(item.id)"
              >
                <span :class="['chat-icon', item.kind]"
                  ><el-icon><ChatDotRound /></el-icon
                ></span>
                <div class="instance-info">
                  <b>{{ item.id }}</b
                  ><small
                    >{{
                      item.kind === "private"
                        ? "私聊 · 直接回复"
                        : "群聊 · 按配置参与"
                    }}<span>
                      {{ item.reply_model || "未配置模型" }}</span
                    ></small
                  >
                </div>
                <span :class="['state-pill', { enabled: item.enabled }]">{{
                  item.enabled ? "已启用" : "已关闭"
                }}</span
                ><el-icon class="row-arrow"><ArrowRight /></el-icon>
              </button>
              <div class="panel-footnote">
                启用状态来自配置；实际监听以机器人运行状态为准。
              </div>
            </section>
            <section class="panel system-panel">
              <div class="panel-heading">
                <div>
                  <h2>连接与服务</h2>
                  <small>当前节点的运行环境</small>
                </div>
                <el-icon><CircleCheck /></el-icon>
              </div>
              <div class="connection-row">
                <span>最近登录记录</span
                ><b>{{
                  loginLabel[snapshot?.runtime?.login_phase] ||
                  snapshot?.runtime?.login_phase ||
                  "状态未知"
                }}</b>
              </div>
              <div class="connection-row">
                <span>微信会话进程</span
                ><b>{{
                  serviceLabel[
                    snapshot?.services?.["wechat"]?.state
                  ] || "状态未知"
                }}</b>
              </div>
              <div class="connection-row">
                <span>自动恢复</span
                ><b>{{
                  serviceLabel[
                    snapshot?.services?.["recovery"]?.state
                  ] || "状态未知"
                }}</b>
              </div>
              <div class="connection-row">
                <span>配置校验</span
                ><b>{{
                  snapshot?.runtime?.config_valid ? "有效" : "需要检查"
                }}</b>
              </div>
              <div class="system-note">
                <span>配置、运行，各有所属。</span>
                <p>关闭管理网页不会停止机器人；停止机器人也不会关闭微信。</p>
              </div>
              <el-button class="full-width" @click="navigate('services')"
                >前往服务管理 <el-icon><ArrowRight /></el-icon
              ></el-button>
            </section>
          </div>
          <section class="panel events-panel">
            <div class="panel-heading">
              <div>
                <h2>最近运行事件</h2>
                <small>只显示安全的事件元数据，不包含聊天正文或密钥</small>
              </div>
              <span class="tiny-label">LATEST EVENTS</span>
            </div>
            <div v-if="!snapshot?.events?.length" class="empty-state">
              暂无可读取的运行事件
            </div>
            <div
              v-for="(event, index) in snapshot?.events?.slice(0, 5)"
              :key="index"
              class="event-line"
            >
              <i></i><code>{{ event }}</code>
            </div>
          </section>
        </template>

        <section v-else-if="page === 'instances'" class="panel instances-panel">
          <div class="table-toolbar">
            <el-input
              v-model="filter"
              placeholder="搜索实例、会话 ID 或模型"
              clearable
              class="search-input"
            /><el-radio-group v-model="statusFilter"
              ><el-radio-button value="all">全部</el-radio-button
              ><el-radio-button value="enabled">已启用</el-radio-button
              ><el-radio-button value="disabled"
                >已关闭</el-radio-button
              ></el-radio-group
            >
          </div>
          <el-table :data="filtered" class="instances-table"
            ><el-table-column label="会话实例" min-width="200"
              ><template #default="{ row }"
                ><div class="table-instance">
                  <span :class="['chat-icon', row.kind]"
                    ><el-icon><ChatDotRound /></el-icon
                  ></span>
                  <div>
                    <b>{{ row.id }}</b
                    ><small>{{
                      row.kind === "private" ? "私聊" : "群聊"
                    }}</small>
                  </div>
                </div></template
              ></el-table-column
            ><el-table-column label="运行状态" min-width="155"
              ><template #default="{ row }">
                <span
                  :class="[
                    'state-pill',
                    { enabled: row.runtime_state === 'running' },
                  ]"
                  >{{ instanceLabels[row.runtime_state] || "未知" }}</span
                >
                <small v-if="row.error" class="runtime-error">{{
                  instanceErrors[row.error] || row.error
                }}</small>
                <small v-else-if="row.config_pending" class="runtime-pending"
                  >有待应用配置</small
                >
              </template></el-table-column
            ><el-table-column
              prop="chat"
              label="会话 ID"
              min-width="210"
              show-overflow-tooltip
            /><el-table-column
              prop="reply_model"
              label="回复模型"
              min-width="180"
            /><el-table-column label="配置状态" width="110"
              ><template #default="{ row }"
                ><span :class="['state-pill', { enabled: row.enabled }]">{{
                  row.enabled ? "已启用" : "已关闭"
                }}</span></template
              ></el-table-column
            ><el-table-column label="队列快照" width="100"
              ><template #default="{ row }">{{
                row.queue_length
              }}</template></el-table-column
            ><el-table-column label="操作" width="270" fixed="right"
              ><template #default="{ row }"
                ><el-button link type="primary" @click="editInstance(row.id)"
                  >模型/配置</el-button
                >
                <el-button link type="primary" @click="showStatus(row.id)"
                  >状态</el-button
                >
                <el-button
                  link
                  :type="shouldStopInstance(row) ? 'danger' : 'primary'"
                  :disabled="
                    instanceBusy === row.id ||
                    !!instanceRequests[row.id] ||
                    row.runtime_state === 'stopping' ||
                    !snapshot?.capabilities?.instance_control
                  "
                  @click="
                    instanceAction(
                      row,
                      shouldStopInstance(row) ? 'stop' : 'start',
                    )
                  "
                  >{{
                    instanceRequests[row.id]
                      ? "等待确认"
                      : shouldStopInstance(row)
                        ? "停用"
                        : "启用"
                  }}</el-button
                >
                <el-button
                  v-if="row.enabled && row.config_pending"
                  link
                  type="primary"
                  :disabled="
                    instanceBusy === row.id ||
                    !!instanceRequests[row.id] ||
                    row.runtime_state === 'stopping' ||
                    !snapshot?.capabilities?.instance_control
                  "
                  @click="instanceAction(row, 'apply')"
                  >应用此实例</el-button
                >
              </template></el-table-column
            ></el-table
          >
          <div class="panel-footnote">
            启停与模型应用只作用于选中实例。账号服务已停止时，启用配置会等待下次启动；配置状态与实际运行状态分别显示。
          </div>
        </section>

        <div v-else-if="page === 'prompts'" class="prompt-layout">
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

        <template v-else-if="page === 'services'"
          ><section class="panel lifecycle-panel">
            <div>
              <span class="eyebrow">BOT LIFECYCLE</span>
              <h2>{{ stateLabel[snapshot?.runtime?.state] || "状态未知" }}</h2>
              <p>机器人可独立启停；微信和管理台由同一主控保持运行。</p>
              <small v-if="!snapshot?.capabilities?.service_control"
                >当前环境未连接机器人主控，暂时无法执行启停。</small
              >
            </div>
            <div class="service-buttons">
              <el-button
                type="primary"
                :disabled="!canControl || running"
                @click="action('start')"
                >启动机器人</el-button
              ><el-button :disabled="!canControl" @click="action('apply')"
                >重启应用全部配置</el-button
              ><el-button
                type="danger"
                plain
                :disabled="
                  !canControl ||
                  snapshot?.services?.['bot']?.state ===
                    'inactive'
                "
                @click="action('stop')"
                >停止机器人</el-button
              >
            </div>
          </section>
          <section class="panel">
            <div class="panel-heading">
              <div>
                <h2>运行组件</h2>
                <small
                  >所有组件由 wxbridge.service 统一管理。</small
                >
              </div>
            </div>
            <div
              v-for="(service, name) in snapshot?.services"
              :key="name"
              class="service-row"
            >
              <span class="service-icon"
                ><el-icon><Connection /></el-icon
              ></span>
              <div>
                <b>{{ componentLabel[String(name)] || name }}</b>
                ><small>PID {{ service.pid || "—" }}</small>
              </div>
              <span
                :class="['state-pill', { enabled: service.state === 'active' }]"
                >{{ serviceLabel[service.state] || service.state }}</span
              >
            </div>
          </section>
          <section class="panel operations-panel">
            <div class="panel-heading">
              <div>
                <h2>操作记录</h2>
                <small>服务操作在后台执行，页面关闭后仍会完成。</small>
              </div>
            </div>
            <div v-if="!snapshot?.operations?.length" class="empty-state">
              暂无本次管理服务的操作记录
            </div>
            <div
              v-for="op in snapshot?.operations"
              :key="op.id"
              class="operation-row"
            >
              <span :class="['state-pill', { enabled: op.state === 'done' }]">{{
                op.state === "running"
                  ? "执行中"
                  : op.state === "done"
                    ? "已完成"
                    : "失败"
              }}</span>
              <div>
                <b>{{ op.message }}</b
                ><small>{{ formatTime(op.started_at) }}</small>
              </div>
            </div>
          </section></template
        >
        <footer class="workspace-footer">
          <span>WXBRIDGE CONSOLE</span
          ><span>连接每一段会话，保留各自的节奏。</span>
        </footer>
      </main>
    </div>
  </div>

  <el-dialog
    v-model="editorOpen"
    :title="creating ? '新建会话实例' : '编辑实例 · ' + editorId"
    width="680px"
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
            <el-form-item label="实例 ID"
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
          <el-form-item label="会话 ID"
            ><el-input
              v-model="editor.chat"
              :disabled="!creating"
              placeholder="wxid_... 或 ...@chatroom"
            /><small class="field-hint"
              >使用真实微信 ID，不使用昵称。每个会话只能绑定一个实例。</small
            ></el-form-item
          >
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
            ><el-form-item label="自动回复 / 参与"
              ><el-switch
                v-model="editor.participation.enabled"
                active-text="启用"
            /></el-form-item>
          </div>
          <small class="field-hint"
            >私聊直接回复，无需参与判断；群聊由参与模型决策。列表来自当前连接的模型目录，也可手动输入模型
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
              <el-form-item label="允许文生图"><el-switch v-model="editor.image_generation.enabled" active-text="启用" /></el-form-item>
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
              <small class="field-hint">用户明确要求绘图时由回复模型调用。任务独立排队，生成后发送到当前会话；每次一个任务，仅支持文生图。模型目录也可能包含文字模型，请选择支持生图的型号。</small>
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
  <el-drawer
    v-model="statusOpen"
    :title="'实例状态 · ' + statusId"
    size="min(520px, 95vw)"
  >
    <template v-if="statusInstance">
      <div class="instance-status-heading">
        <span
          :class="[
            'state-pill',
            { enabled: statusInstance.runtime_state === 'running' },
          ]"
          >{{
            instanceLabels[statusInstance.runtime_state] || "状态未知"
          }}</span
        ><small>{{
          statusInstance.snapshot_current
            ? "当前进程实时快照"
            : "当前无有效运行快照"
        }}</small>
      </div>
      <el-alert
        v-if="statusInstance.error"
        :title="instanceErrors[statusInstance.error] || statusInstance.error"
        type="error"
        :closable="false"
      />
      <el-descriptions :column="1" border>
        <el-descriptions-item label="会话 ID">{{
          statusInstance.chat
        }}</el-descriptions-item>
        <el-descriptions-item label="配置开关">{{
          statusInstance.enabled ? "已启用" : "已停用"
        }}</el-descriptions-item>
        <el-descriptions-item label="已配置回复模型">{{
          statusInstance.reply_model || "未配置"
        }}</el-descriptions-item>
        <el-descriptions-item label="当前运行回复模型">{{
          statusInstance.snapshot_current
            ? statusInstance.active_models?.reply || "—"
            : "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="配置生效">{{
          !statusInstance.snapshot_current
            ? "已保存，等待运行层确认"
            : statusInstance.config_pending
              ? "有待应用修改"
              : "当前配置已确认"
        }}</el-descriptions-item>
        <el-descriptions-item label="队列快照">{{
          statusInstance.queue_length
        }}</el-descriptions-item>
        <el-descriptions-item label="消息游标">{{
          statusInstance.cursor ?? "—"
        }}</el-descriptions-item>
        <el-descriptions-item label="图片生成">{{
          !statusInstance.snapshot_current ? '暂无运行快照' : statusInstance.image_generation?.enabled
            ? '已启用 · 运行中 ' + (statusInstance.image_generation.running || 0) : '已关闭'
        }}</el-descriptions-item>
        <el-descriptions-item label="最近生图">{{
          statusInstance.image_generation?.last?.error || ({ready: '已生成，等待发送', confirmed: '已确认发出', failed: '生成失败'} as Record<string, string>)[statusInstance.image_generation?.last?.state] || '暂无'
        }}</el-descriptions-item>
        <el-descriptions-item label="最近发送成功">{{
          formatTime(statusInstance.last_success)
        }}</el-descriptions-item>
        <el-descriptions-item label="聊天 JSON 预算">{{
          statusInstance.snapshot_current ? (statusInstance.context_policy?.context_max_chars ?? "—") + " 字符" : "暂无运行快照"
        }}</el-descriptions-item>
        <el-descriptions-item label="最近参与上下文">{{
          statusInstance.snapshot_current ? contextSummary(statusInstance.context?.participation) : "暂无运行快照"
        }}</el-descriptions-item>
        <el-descriptions-item label="最近回复上下文">{{
          statusInstance.snapshot_current ? contextSummary(statusInstance.context?.reply) : "暂无运行快照"
        }}</el-descriptions-item>
        <el-descriptions-item label="发送阻塞">{{
          statusInstance.blocked_reason || "无"
        }}</el-descriptions-item>
      </el-descriptions>
      <h3 class="channel-heading">模型通道</h3>
      <div
        v-for="(channel, name) in statusInstance.channels"
        :key="name"
        class="channel-status"
      >
        <b>{{ componentLabel[String(name)] || name }}</b>
        ><span
          >{{ channel.running ? "执行中" : "空闲" }} · 排队
          {{ channel.queued }}</span
        ><small>{{
          channel.client_created ? "client 已创建" : "client 尚未创建"
        }}</small>
      </div>
      <div
        v-if="!Object.keys(statusInstance.channels || {}).length"
        class="empty-state"
      >
        尚无模型通道快照
      </div>
    </template>
  </el-drawer>
</template>
