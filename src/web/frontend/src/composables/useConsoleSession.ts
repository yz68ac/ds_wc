import { onScopeDispose, ref } from "vue";
import { api, setCsrf } from "../api.ts";

type Options = {
  afterEnter?: () => Promise<void>;
  onPoll?: () => void;
  onError?: (error: any) => void;
  request?: typeof api;
  eventSource?: (url: string) => EventSource;
};

export function useConsoleSession(options: Options = {}) {
  const request = options.request || api;
  const authenticated = ref(false), initialized = ref(false), demo = ref(false);
  const password = ref(""), loginBusy = ref(false), loginError = ref("");
  const snapshot = ref<any>(null), connected = ref(false), loading = ref(false), error = ref("");
  let source: EventSource | null = null;
  let timer: ReturnType<typeof setInterval> | null = null;
  let epoch = 0, disposed = false;

  function expire() {
    epoch++;
    source?.close(); source = null;
    authenticated.value = false; connected.value = false; snapshot.value = null;
    loading.value = false;
    setCsrf("");
  }
  async function refresh() {
    const generation = epoch;
    loading.value = true;
    try {
      const value = await request("/overview");
      if (disposed || generation !== epoch) return;
      snapshot.value = value; error.value = "";
    } catch (failure: any) {
      if (disposed || generation !== epoch) return;
      error.value = failure.message;
      if (failure.status === 401) { expire(); options.onError?.(failure); }
    } finally { if (generation === epoch) loading.value = false; }
  }
  function subscribe() {
    source?.close();
    const generation = epoch;
    source = (options.eventSource || ((url) => new EventSource(url)))("/api/events");
    source.addEventListener("snapshot", (event: MessageEvent) => {
      if (disposed || generation !== epoch) return;
      try { snapshot.value = JSON.parse(event.data); connected.value = true; error.value = ""; }
      catch { connected.value = false; error.value = "运行快照格式无效"; }
    });
    source.addEventListener("expired", () => { if (generation === epoch) expire(); });
    source.onerror = () => { if (generation === epoch) connected.value = false; };
  }
  async function enter() {
    const generation = epoch;
    await refresh();
    if (disposed || generation !== epoch || !authenticated.value) return;
    await options.afterEnter?.();
    if (!disposed && generation === epoch && authenticated.value) subscribe();
  }
  async function login() {
    const generation = ++epoch;
    loginBusy.value = true; loginError.value = "";
    try {
      const value = await request("/login", {method:"POST", body:JSON.stringify({password:password.value})});
      if (disposed || generation !== epoch) return;
      authenticated.value = true; demo.value = value.demo; setCsrf(value.csrf);
      password.value = "";
      await enter();
    } catch (failure: any) { if (!disposed && generation === epoch) loginError.value = failure.message; }
    finally { loginBusy.value = false; }
  }
  async function logout() {
    await request("/session", {method:"DELETE"});
    expire();
  }
  async function initialize() {
    const generation = epoch;
    try {
      const value = await request("/session");
      if (disposed || generation !== epoch) return;
      demo.value = value.demo; authenticated.value = value.authenticated; setCsrf(value.csrf || "");
      if (value.authenticated) await enter();
    } catch (failure: any) { if (!disposed) loginError.value = failure.message; }
    finally { initialized.value = true; }
    if (!disposed && !timer) timer = setInterval(() => {
      if (!authenticated.value) return;
      if (!connected.value) void refresh();
      options.onPoll?.();
    }, 5000);
  }
  function dispose() {
    disposed = true; expire();
    if (timer) clearInterval(timer);
    timer = null;
  }
  onScopeDispose(dispose);
  return {authenticated, initialized, demo, password, loginBusy, loginError, snapshot,
          connected, loading, error, refresh, login, logout, initialize, expire, dispose};
}
