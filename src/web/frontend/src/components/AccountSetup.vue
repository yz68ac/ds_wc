<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { ArrowRight, ChatDotRound, CircleCheck, Connection, Iphone, Refresh } from '@element-plus/icons-vue';
import { api } from '../api';

const props = defineProps<{ snapshot: any }>();
const emit = defineEmits<{ navigate: [page: string]; refresh: []; error: [error: any] }>();
const dialog = ref(false), login = ref<any>(null), loginBusy = ref(false), binding = ref(false);
const loginError = ref(''), bindingError = ref('');
const setup = computed(() => props.snapshot?.account_setup || {});
const phase = computed(() => login.value?.phase || props.snapshot?.runtime?.login_phase || 'unknown');
const loggedIn = computed(() => ['logged_in', 'setup_required'].includes(phase.value));
const count = computed(() => setup.value.instance_count || 0);
const pending = computed<string[]>(() => setup.value.pending_instances || []);
const authorized = computed(() => setup.value.bound && count.value > 0 && pending.value.length === 0);
const canBind = computed(() => setup.value.can_bind && loggedIn.value && !binding.value);
const labels: Record<string, string> = {
  logged_in: '微信已登录', setup_required: '微信已登录，等待绑定', qr_required: '等待扫码',
  qr_expired: '二维码已过期', phone_confirmation: '等待手机确认', confirmation_pending: '等待手机确认',
  login_ready: '正在准备登录', open_ready: '正在进入微信', opening_wechat: '正在进入微信',
  client_stopped: '微信尚未运行', waiting_manual: '需要在微信端确认', completed: '正在核对登录状态',
  waiting: '正在连接微信', unknown: '等待获取登录状态',
};
const phaseLabel = computed(() => labels[phase.value] || '正在核对登录状态');
const bindingHint = computed(() => !loggedIn.value ? '先完成微信登录，再绑定账号和会话。'
  : !setup.value.can_bind ? '账号服务正在运行或排空，请先到服务管理停止机器人。'
  : !count.value ? '可以先绑定账号，再创建会话实例。'
  : pending.value.length ? `${pending.value.join('、')} 尚未获得发送授权。`
  : '账号和当前实例的会话授权已配置，可前往服务管理。');
let timer: ReturnType<typeof setTimeout> | undefined;
let generation = 0, failures = 0, disposed = false;
function stopPolling() { generation++; if (timer) clearTimeout(timer); timer = undefined; }
function schedule() {
  if (disposed) return;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { timer = undefined; if (dialog.value) void fetchLogin(); }, 4000);
}
async function fetchLogin() {
  if (loginBusy.value || !dialog.value) return;
  if (document.hidden) { schedule(); return; }
  const current = generation;
  loginBusy.value = true;
  try {
    const result = await api('/account/login', { method: 'POST', body: '{}' });
    if (current !== generation) return;
    login.value = result;
    loginError.value = ''; failures = 0;
    if (['logged_in', 'setup_required'].includes(result.phase)) emit('refresh');
    else schedule();
  } catch (e: any) {
    if (current !== generation) return;
    login.value = null;
    loginError.value = e.message || '暂时无法获取二维码，请稍后重试。';
    if (e.status === 401) { dialog.value = false; emit('error', e); }
    else if (++failures < 3) schedule();
  } finally {
    loginBusy.value = false;
    if (current !== generation && dialog.value) schedule();
  }
}
function openLogin() { login.value = null; dialog.value = true; }
function retryLogin() { stopPolling(); failures = 0; void fetchLogin(); }
watch(dialog, (open) => {
  stopPolling(); failures = 0; loginError.value = '';
  if (open) void fetchLogin();
});
onBeforeUnmount(() => { disposed = true; stopPolling(); });
async function bindAccount() {
  if (!canBind.value) return;
  try {
    await ElMessageBox.confirm(count.value
      ? `核对当前微信账号，并为 ${count.value} 个实例配置会话发送授权。请确认当前账号拥有这些群或联系人。实例不会自动启用，机器人不会自动启动。`
      : '将当前登录的微信账号绑定到此运行环境，机器人保持停止。', '绑定账号与会话',
      { confirmButtonText: '确认绑定', cancelButtonText: '取消', type: 'info' });
  } catch { return; }
  binding.value = true; bindingError.value = '';
  try {
    const result = await api('/account/bind', { method: 'POST', body: '{}' });
    ElMessage.success(result.message);
    emit('refresh');
  } catch (e: any) {
    bindingError.value = e.message;
    if (e.status === 401) emit('error', e);
  } finally { binding.value = false; }
}
</script>

<template>
  <section class="account-setup" aria-labelledby="account-setup-title">
    <div class="setup-heading">
      <div class="setup-title">
        <span class="setup-symbol"><el-icon><ChatDotRound /></el-icon></span>
        <div><h3 id="account-setup-title">微信账号</h3><p>管理登录状态、会话实例与发送授权。</p></div>
      </div>
      <span :class="['setup-status', { ready: loggedIn }]" role="status"><i></i>{{ phaseLabel }}</span>
    </div>
    <div class="setup-steps">
      <article :class="['setup-step', { complete: loggedIn }]">
        <div class="step-kicker"><span class="step-number"><el-icon v-if="loggedIn"><CircleCheck /></el-icon><template v-else>01</template></span><span>微信登录</span></div>
        <h4>{{ loggedIn ? '账号已连接' : '扫码连接微信' }}</h4>
        <p>{{ loggedIn ? '微信会话已就绪，登录状态保留在当前账号目录。' : '使用手机微信扫码，并在手机上确认登录。' }}</p>
        <el-button :icon="loggedIn ? Refresh : Iphone" @click="openLogin">{{ loggedIn ? '检查登录状态' : '打开登录二维码' }}</el-button>
      </article>
      <article :class="['setup-step', { complete: count > 0 }]">
        <div class="step-kicker"><span class="step-number"><el-icon v-if="count"><CircleCheck /></el-icon><template v-else>02</template></span><span>会话实例</span></div>
        <h4>{{ count ? `已配置 ${count} 个实例` : '为会话配置机器人' }}</h4>
        <p>{{ count ? `${snapshot.instances.filter((i: any) => i.enabled).length} 个已启用。每个实例独立设置模型、提示词和记忆。` : '选择群聊或联系人，配置回复模型和提示词。' }}</p>
        <el-button :icon="ChatDotRound" @click="emit('navigate', 'instances')">{{ count ? '管理会话实例' : '创建会话实例' }}</el-button>
      </article>
      <article :class="['setup-step', { complete: authorized }]">
        <div class="step-kicker"><span class="step-number"><el-icon v-if="authorized"><CircleCheck /></el-icon><template v-else>03</template></span><span>账号与授权</span></div>
        <h4>{{ authorized ? '会话授权已就绪' : !setup.bound ? '绑定当前账号' : `还有 ${pending.length} 个实例待授权` }}</h4>
        <p>{{ bindingHint }}</p>
        <el-button :type="authorized ? 'default' : 'primary'" :icon="Connection" :disabled="!canBind" :loading="binding" @click="bindAccount">{{ authorized ? '重新核对授权' : '绑定账号与会话' }}</el-button>
      </article>
    </div>
    <el-alert v-if="bindingError" :title="bindingError" type="error" show-icon :closable="false" />
    <div class="setup-footer"><span>新增或更换实例会话后，需要重新核对授权。</span><button class="text-link" @click="emit('navigate', 'services')">{{ setup.can_bind ? '前往服务管理' : '管理账号服务' }}<el-icon><ArrowRight /></el-icon></button></div>
  </section>

  <el-dialog v-model="dialog" title="连接微信" width="min(440px, calc(100vw - 32px))" align-center class="account-login-dialog" :close-on-click-modal="false" @closed="!dialog && (login = null)">
    <div class="login-viewer" aria-live="polite">
      <div v-if="loggedIn && !loginBusy && login" class="login-success"><el-icon><CircleCheck /></el-icon><h3>微信登录成功</h3><p>可以继续配置实例并绑定账号。</p></div>
      <template v-else>
        <div class="qr-frame" v-loading="loginBusy && !login?.qr">
          <img v-if="login?.qr && !loginError" :src="login.qr" alt="使用手机微信扫描此二维码登录" />
          <div v-else class="qr-placeholder"><el-icon><Iphone /></el-icon><span>{{ loginBusy ? '正在获取登录状态' : phaseLabel }}</span></div>
        </div>
        <h3>{{ phaseLabel }}</h3>
        <p>{{ ['phone_confirmation', 'confirmation_pending'].includes(phase) ? '请打开手机微信，确认本次登录。' : '二维码和登录状态每 4 秒自动更新，扫码后请在手机确认。' }}</p>
      </template>
      <el-alert v-if="loginError" :title="loginError" type="warning" show-icon :closable="false" />
    </div>
    <template #footer><el-button @click="dialog = false">{{ loggedIn ? '完成' : '稍后登录' }}</el-button><el-button :icon="Refresh" :loading="loginBusy" @click="retryLogin">刷新状态</el-button></template>
  </el-dialog>
</template>

<style scoped>
.account-setup { margin-bottom: 24px; padding: 24px; border: 1px solid var(--ui-border); border-radius: 16px; background: #fff; box-shadow: var(--ui-shadow); }
.setup-heading,.setup-title,.setup-footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.setup-title { justify-content: flex-start; }
.setup-title h3 { font-size: 18px; font-weight: 650; letter-spacing: .3px; }
.setup-title p { margin-top: 5px; font-size: 13px; color: var(--ui-muted); }
.setup-symbol { display: grid; place-items: center; flex: 0 0 44px; height: 44px; border-radius: 12px; background: var(--ui-accent-soft); color: var(--ui-accent); font-size: 24px; }
.setup-status { display: inline-flex; align-items: center; gap: 7px; padding: 7px 11px; background: #f5f3ed; border-radius: 20px; color: #8a7854; font-size: 12px; white-space: nowrap; }
.setup-status i { width: 6px; height: 6px; background: currentColor; border-radius: 50%; }
.setup-status.ready { color: var(--ui-accent); background: var(--ui-accent-soft); }
.setup-steps { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 0; margin: 22px 0 18px; border: 1px solid var(--ui-border); border-radius: 12px; overflow: hidden; }
.setup-step { padding: 19px 20px; min-width: 0; border-right: 1px solid var(--ui-border); background: #fff; display: flex; flex-direction: column; align-items: flex-start; }
.setup-step:last-child { border-right: 0; }

.step-kicker { display: flex; align-items: center; gap: 8px; color: var(--ui-muted); font-size: 12px; }
.step-number { display: grid; place-items: center; width: 25px; height: 25px; border: 1px solid #dce3db; border-radius: 50%; font-size: 10px; font-weight: 600; }
.complete .step-number { color: var(--ui-accent); background: #e9f3ec; border-color: #d4e6d8; font-size: 17px; }
.setup-step h4 { margin: 13px 0 7px; font-size: 15px; font-weight: 600; }
.setup-step p { color: var(--ui-secondary); font-size: 13px; line-height: 1.65; margin-bottom: 16px; overflow-wrap: anywhere; }
.setup-step .el-button { margin-top: auto; }
.setup-footer { color: var(--ui-muted); font-size: 12px; line-height: 1.6; }
.account-setup > .el-alert { margin-bottom: 15px; }
.login-viewer { text-align: center; }
.qr-frame { width: 248px; height: 248px; margin: 4px auto 22px; padding: 12px; background: #fff; border: 1px solid #e3e9e1; border-radius: 14px; }
.qr-frame img { width: 100%; height: 100%; object-fit: contain; }
.qr-placeholder { height: 100%; display: flex; flex-direction: column; justify-content: center; gap: 16px; align-items: center; color: #91a093; font-size: 12px; background: #f8faf6; border-radius: 8px; }
.qr-placeholder > .el-icon { font-size: 40px; }
.login-viewer h3 { font-size: 18px; }
.login-viewer p { margin: 10px 10px 20px; color: var(--ui-muted); line-height: 1.8; font-size: 13px; }
.login-success { padding: 25px 0 10px; }
.login-success > .el-icon { color: var(--ui-accent); font-size: 64px; margin-bottom: 20px; }
@media (max-width: 900px) { .account-setup { padding: 18px; } .setup-step { padding: 16px; } }
@media (max-width: 640px) { .setup-heading { align-items: flex-start; flex-direction: column; } .setup-steps { grid-template-columns: 1fr; margin-top: 18px; } .setup-step { border-right: 0; border-bottom: 1px solid var(--ui-border); } .setup-step:last-child { border-bottom: 0; } .setup-step p { margin-bottom: 14px; } .setup-footer { align-items: flex-start; flex-direction: column; gap: 10px; } }
</style>
