import { toRaw } from "vue";

export type Configuration = Record<string, any>;

// Configuration comes from JSON, but Vue may wrap any nested value in a proxy.
// Clone it recursively so reactive arrays and objects never reach structuredClone.
function cloneConfiguration(value: any, seen = new WeakMap<object, any>()): any {
  const raw = toRaw(value);
  if (raw === null || typeof raw !== "object") return structuredClone(raw);
  const existing = seen.get(raw);
  if (existing !== undefined) return existing;
  const prototype = Object.getPrototypeOf(raw);
  if (!Array.isArray(raw) && prototype !== Object.prototype && prototype !== null)
    return structuredClone(raw);
  const result = Array.isArray(raw) ? new Array(raw.length) : {};
  seen.set(raw, result);
  for (const [key, entry] of Object.entries(raw)) {
    Object.defineProperty(result, key, {
      value: cloneConfiguration(entry, seen), enumerable: true, writable: true, configurable: true,
    });
  }
  return result;
}

function merge(...values: Configuration[]): Configuration {
  const result: Configuration = Object.create(null);
  for (const value of values) for (const [key, entry] of Object.entries(value || {})) {
    result[key] = entry && typeof entry === "object" && !Array.isArray(entry)
      ? merge(result[key] && typeof result[key] === "object" ? result[key] : {}, entry)
      : cloneConfiguration(entry);
  }
  return result;
}
export function prepareInstanceEditor(config: Configuration, effective: Configuration = {}, defaults: Configuration = {}): Configuration {
  const value = cloneConfiguration(config);
  const resolved = merge(defaults, effective, config);
  for (const key of ["model_clients", "models", "participation"]) value[key] = resolved[key] || {};
  for (const key of ["memory", "profile", "image_generation"]) value[key] = { enabled: resolved[key]?.enabled ?? false, ...value[key] };
  value.memory.avatar = { enabled: resolved.memory?.avatar?.enabled ?? false, ...value.memory.avatar };
  return value;
}
