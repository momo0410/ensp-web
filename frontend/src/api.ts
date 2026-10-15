// API 客户端 + SSE 流式读取
export interface ApiOptions extends Omit<RequestInit, "headers"> {
  headers?: Record<string, string>
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export async function api<T = any>(path: string, options: ApiOptions = {}): Promise<T> {
  const config: RequestInit = {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  }
  const resp = await fetch(path, config)
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
): Promise<string> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
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

/** Agent 执行轨迹事件（后端 /api/agent/chat 的 SSE 载荷）。 */
export interface AgentEvent {
  type: "start" | "tool_call" | "tool_result" | "answer" | "error" | "done"
  step?: number
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any
}

/**
 * Agent 流式请求：逐条抛出结构化事件（工具调用 / 工具结果 / 最终答案）。
 * 与 streamSSE 的区别在于事件是**有类型的对象**，供轨迹面板逐步渲染。
 */
export async function streamAgent(
  url: string,
  body: unknown,
  onEvent: (ev: AgentEvent) => void,
): Promise<AgentEvent[]> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!resp.ok || !resp.body) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const err: any = await resp.json().catch(() => ({}))
    throw new Error(err.detail || `HTTP ${resp.status}`)
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  const events: AgentEvent[] = []
  let buf = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 2)
      if (!raw.startsWith("data:")) continue
      let payload: AgentEvent
      try { payload = JSON.parse(raw.slice(5).trim()) } catch { continue }
      events.push(payload)
      onEvent(payload)
    }
  }
  return events
}
