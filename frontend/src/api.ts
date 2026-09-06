// API 客户端：访问令牌注入 + 401 弹窗重试 + SSE 流式读取
export function getToken(): string {
  return localStorage.getItem("web_access_token") || ""
}

export function setStoredToken(t: string): void {
  try { localStorage.setItem("web_access_token", t) } catch (e) { /* ignore */ }
}

let _tokenWaiters: Array<(v: string) => void> = []

/** 请求 UI 弹出令牌输入框；用户提交后经 resolveAccessToken(value) 回填（并发请求全部放行） */
export function promptAccessToken(): Promise<string> {
  window.dispatchEvent(new Event("ensp:token-required"))
  return new Promise((resolve) => { _tokenWaiters.push(resolve) })
}

export function resolveAccessToken(value: string): void {
  const waiters = _tokenWaiters
  _tokenWaiters = []
  for (const r of waiters) r(value)
}

export interface ApiOptions extends Omit<RequestInit, "headers"> {
  headers?: Record<string, string>
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function api<T = any>(path: string, options: ApiOptions = {}, _retried = false): Promise<T> {
  const config: RequestInit = {
    ...options,
    headers: { "Content-Type": "application/json", "X-Access-Token": getToken(), ...(options.headers || {}) },
  }
  const resp = await fetch(path, config)
  if (resp.status === 401 && !_retried) {
    const t = await promptAccessToken()
    if (!t) throw new Error("需要访问令牌：请在弹窗中输入服务端打印的令牌")
    return api<T>(path, options, true)
  }
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const err: any = await resp.json()
      detail = err.detail || err.message || detail
    } catch (_) { /* 忽略解析失败 */ }
    throw new Error(detail)
  }
  return resp.json()
}

/**
 * SSE 流式请求（AI 对话 / 实验提示）。
 * 事件格式：data: {"delta": "..."} 增量 / data: {"error": "..."} 错误，以空行分帧。
 * 流式期间通过 onDelta(累计回复) 回调；结束后返回完整回复文本。
 */
export async function streamSSE(
  url: string,
  body: unknown,
  onDelta: (reply: string) => void,
  _retried = false,
): Promise<string> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Access-Token": getToken() },
    body: JSON.stringify(body),
  })
  if (resp.status === 401 && !_retried) {
    const t = await promptAccessToken()
    if (!t) throw new Error("需要访问令牌：请在弹窗中输入服务端打印的令牌")
    return streamSSE(url, body, onDelta, true)
  }
  if (!resp.ok || !resp.body) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const err: any = await resp.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${resp.status}`)
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = "", reply = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 2)
      if (!raw.startsWith("data:")) continue
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let payload: any
      try { payload = JSON.parse(raw.slice(5).trim()) } catch (e) { continue }
      if (payload.error) throw new Error(payload.error)
      if (payload.delta) {
        reply += payload.delta
        onDelta(reply)
      }
    }
  }
  return reply
}
