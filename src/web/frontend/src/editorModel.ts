export type Configuration = Record<string, any>;
function merge(...values: Configuration[]): Configuration {
  const result: Configuration = Object.create(null);
  for (const value of values) for (const [key, entry] of Object.entries(value || {})) {
    result[key] = entry && typeof entry === "object" && !Array.isArray(entry)
      ? merge(result[key] && typeof result[key] === "object" ? result[key] : {}, entry)
      : structuredClone(entry);
  }
  return result;
}
export function prepareInstanceEditor(config: Configuration, effective: Configuration = {}, defaults: Configuration = {}): Configuration {
  const value = structuredClone(config);
  const resolved = merge(defaults, effective, config);
  for (const key of ["model_clients", "models", "participation"]) value[key] = resolved[key] || {};
  for (const key of ["memory", "profile", "image_generation"]) value[key] = { enabled: resolved[key]?.enabled ?? false, ...value[key] };
  value.memory.avatar = { enabled: resolved.memory?.avatar?.enabled ?? false, ...value.memory.avatar };
  return value;
}
