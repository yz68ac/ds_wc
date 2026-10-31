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
  const data = await response.json();
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
