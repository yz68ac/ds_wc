let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}

export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...options.headers,
    },
  });
  // Proxies and gateways may answer with HTML or an empty body; never surface a raw SyntaxError.
  const text = await response.text();
  let data: any = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      const error = new Error(
        response.ok ? "管理服务返回了无法解析的响应。" : `请求失败（HTTP ${response.status}），请稍后重试。`,
      );
      Object.assign(error, { status: response.status });
      throw error;
    }
  }
  if (!response.ok) {
    const error = new Error(
      data.message ||
        (response.status === 422
          ? "输入格式无效，请检查各字段。"
          : "请求失败，请稍后重试。"),
    );
    Object.assign(error, { status: response.status });
    throw error;
  }
  return data;
}
