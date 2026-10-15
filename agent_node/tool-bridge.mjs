/**
 * tool-bridge.mjs - 工具桥（Node sidecar → Python 工具执行端点）
 * =============================================================
 * Agent 循环跑在 Node（pi），但**工具的实现在 Python**（设备表、Console
 * 连接、能力缓存、下发链路全在那边）。所以每次工具调用都要回调 Python。
 *
 * 安全要点：
 * - 只走 127.0.0.1 回环，不监听外网。
 * - 每次请求带一个进程启动时随机生成的共享密钥；Python 侧校验，
 *   防止本机其它进程伪造工具调用。
 * - **工具集由 Python 决定**：sidecar 从 Python 拉取工具清单（名称 + JSON
 *   Schema），再包装成 pi 的 AgentTool。Node 侧不硬编码任何工具，
 *   因此"能不能下发配置"这件事仍然只在 Python 一处说了算。
 */

const TOOL_ENDPOINT_TIMEOUT_MS = 180_000;

/** 从 Python 拉取工具清单（OpenAI 风格的 function schema）。 */
export async function fetchToolSchemas(pythonBase, token) {
  const resp = await fetch(`${pythonBase}/api/agent/internal/tools`, {
    headers: { "X-Agent-Token": token },
  });
  if (!resp.ok) {
    throw new Error(`拉取工具清单失败 HTTP ${resp.status}: ${await resp.text()}`);
  }
  const data = await resp.json();
  if (!Array.isArray(data.tools)) {
    throw new Error("工具清单格式异常：缺少 tools 数组");
  }
  return data.tools;
}

/** 执行一次工具调用（回调 Python）。任何失败都转成 pi 能读的文本结果。 */
export async function executeToolViaPython(pythonBase, token, name, args) {
  const started = Date.now();
  try {
    const resp = await fetch(`${pythonBase}/api/agent/internal/tools/execute`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Agent-Token": token,
      },
      body: JSON.stringify({ name, args: args ?? {} }),
      signal: AbortSignal.timeout(TOOL_ENDPOINT_TIMEOUT_MS),
    });
    const text = await resp.text();
    if (!resp.ok) {
      // 让模型看到失败原因，而不是静默空结果
      return {
        text: JSON.stringify({
          ok: false,
          error: `工具端点 HTTP ${resp.status}: ${text.slice(0, 500)}`,
        }),
        isError: true,
        durationMs: Date.now() - started,
      };
    }
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      return {
        text: JSON.stringify({ ok: false, error: "工具端点返回了非 JSON" }),
        isError: true,
        durationMs: Date.now() - started,
      };
    }
    return {
      text: typeof payload === "string" ? payload : JSON.stringify(payload),
      isError: payload?.ok === false,
      durationMs: Date.now() - started,
    };
  } catch (e) {
    return {
      text: JSON.stringify({
        ok: false,
        error: `调用工具端点失败: ${e?.message || String(e)}`,
      }),
      isError: true,
      durationMs: Date.now() - started,
    };
  }
}

/**
 * 把 Python 给的 tool schema 包装成 pi 的 AgentTool。
 *
 * pi 的 AgentTool 契约（`@earendil-works/pi-agent-core` types.d.ts:409）：
 *   { label, name, description, parameters, execute, executionMode? }
 *   execute(toolCallId, params, signal?, onUpdate?) => Promise<AgentToolResult>
 *   AgentToolResult = { content: [{type:"text",text}], details, isError?, terminate? }
 *
 * 两个易错点（都踩过）：
 * 1. `execute` 的**第一个参数是 toolCallId，不是 args**；args 在第二个。
 *    签名写成 (args) 会把调用 ID 当成参数发给 Python，后端 422。
 * 2. `label` 与 `details` 是必需的，漏了会让上层在处理结果时报错。
 */
export function buildAgentTools(schemas, pythonBase, token, onTrace) {
  return schemas.map((s) => {
    const fn = s.function ?? s;
    const label = s.kind === "prepare" ? `${fn.name}（产出待确认方案）` : fn.name;
    return {
      label,
      name: fn.name,
      description: fn.description || fn.name,
      parameters: fn.parameters || {
        type: "object",
        properties: {},
        required: [],
        additionalProperties: false,
      },
      // prepare 类工具（产出待确认方案）必须串行：它们会写方案登记表，
      // 并发会打乱顺序也让 plan_id 与展示对不上。
      executionMode: s.kind === "prepare" ? "sequential" : undefined,
      execute: async (toolCallId, params) => {
        const args = params && typeof params === "object" ? params : {};
        if (onTrace) onTrace({ phase: "start", name: fn.name, args, toolCallId });
        const r = await executeToolViaPython(pythonBase, token, fn.name, args);
        if (onTrace) {
          onTrace({
            phase: "end",
            name: fn.name,
            ok: !r.isError,
            text: r.text,
            durationMs: r.durationMs,
          });
        }
        let details;
        try {
          details = JSON.parse(r.text);
        } catch {
          details = { text: r.text };
        }
        return {
          content: [{ type: "text", text: r.text }],
          details,
          isError: r.isError,
        };
      },
    };
  });
}
