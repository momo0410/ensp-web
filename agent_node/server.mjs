/**
 * server.mjs - Agent sidecar（pi 循环跑在这里）
 * =============================================
 * 由 Python 后端拉起，只监听 127.0.0.1 的回环端口，接受两件事：
 *
 *   GET  /health          存活探测
 *   POST /run             跑一轮 Agent，SSE 流式回事件
 *
 * 设计取向（与 AGENTS.md 安全红线一致）：
 * - **工具清单与执行全在 Python**：本进程启动时从 Python 拉工具 schema，
 *   每次调用再回调 Python 执行。这里不硬编码任何工具，因此"Agent 能做什么"
 *   这件事仍然只在 Python 一处定义——包括"它没有任何直接下发的工具"。
 * - 共享密钥：只接受带正确 X-Agent-Token 的请求，避免本机其它进程调用。
 * - 事件契约：把 pi 的原生事件映射成前端已认识的类型
 *   （tool_call / tool_result / answer / error / done），前端无需大改。
 */
import http from "node:http";
import { Agent } from "@earendil-works/pi-agent-core";
import { buildModels } from "./provider.mjs";
import { buildAgentTools, fetchToolSchemas } from "./tool-bridge.mjs";

const PORT = Number(process.env.AGENT_PORT || 0);
const TOKEN = process.env.AGENT_TOKEN || "";
const PYTHON_BASE = process.env.AGENT_PYTHON_BASE || "http://127.0.0.1:8000";

if (!TOKEN) {
  console.error("[sidecar] 缺少 AGENT_TOKEN，拒绝启动");
  process.exit(2);
}
if (!PORT) {
  console.error("[sidecar] 缺少 AGENT_PORT，拒绝启动");
  process.exit(2);
}

const MAX_STEPS = 8;

/** 读请求体（带上限，防止超大 body 打爆内存）。 */
function readBody(req, limit = 4 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > limit) {
        reject(new Error("请求体过大"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

/**
 * 把 pi 的事件映射成前端契约并写 SSE。
 *
 * 关键映射：
 * - tool_execution_start  → tool_call
 * - tool_execution_end    → tool_result
 * - 文本增量累积          → answer（流式，可多次）
 * - 生命周期               → start / done
 */
function makeEmitter(res) {
  const state = {
    text: "",
    toolCalls: [],
    lastArgs: {},
    lastPushAt: 0,
    lastPushLen: 0,
    answerPushed: false,
  };
  return {
    state,
    raw(type, data) {
      res.write(`data: ${JSON.stringify({ type, ...data })}\n\n`);
    },
    push(event) {
      switch (event.type) {
        case "tool_execution_start": {
          state.toolCalls.push(event.toolName);
          state.lastArgs[event.toolName] = event.args;
          this.raw("tool_call", {
            step: state.toolCalls.length,
            tool: event.toolName,
            args: event.args ?? {},
          });
          break;
        }
        case "tool_execution_end": {
          // pi 的 event.result 是 AgentToolResult：{ content:[{type,text}], details, isError? }
          // 我们桥接时把 details 设成工具返回的 JSON，优先取它。
          let payload = event.result?.details ?? null;
          if (payload === null) {
            try {
              const blocks = event.result?.content;
              const textBlock = Array.isArray(blocks)
                ? blocks.find((b) => b?.type === "text" && typeof b.text === "string")
                : null;
              payload = textBlock ? JSON.parse(textBlock.text) : null;
            } catch {
              payload = null;
            }
          }
          if (payload === null) {
            payload = { raw: String(event.result ?? "") };
          }
          this.raw("tool_result", {
            step: state.toolCalls.length,
            tool: event.toolName,
            ok: !event.isError,
            result: payload,
            duration_ms: event.durationMs,
          });
          break;
        }
        case "message_update": {
          const ev = event.assistantMessageEvent;
          if (ev?.type === "text_delta" && ev.delta) {
            state.text += ev.delta;
            // 合并高频增量：每 ~120ms 或每 48 字符推一次，避免一个 token 一个 SSE 帧
            const now = Date.now();
            if (now - state.lastPushAt >= 120 || state.text.length - state.lastPushLen >= 48) {
              state.lastPushAt = now;
              state.lastPushLen = state.text.length;
              this.raw("answer", { answer: state.text, streaming: true });
            }
          }
          break;
        }
        case "message_end": {
          if (event.message?.role === "assistant" && event.message.stopReason === "error") {
            this.raw("error", { error: event.message.errorMessage || "模型调用失败" });
          }
          break;
        }
        default:
          break;
      }
    },
  };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);

  if (req.method === "GET" && url.pathname === "/health") {
    return sendJson(res, 200, { ok: true, pid: process.pid, port: PORT });
  }

  if (req.method !== "POST" || url.pathname !== "/run") {
    return sendJson(res, 404, { error: "not found" });
  }

  if (req.headers["x-agent-token"] !== TOKEN) {
    return sendJson(res, 401, { error: "token 不匹配" });
  }

  let body;
  try {
    body = JSON.parse(await readBody(req));
  } catch (e) {
    return sendJson(res, 400, { error: `请求体解析失败: ${e.message}` });
  }

  const { message, history = [], cfg = {}, mode = "tutor", device_context = "" } = body;
  if (!message || !String(message).trim()) {
    return sendJson(res, 400, { error: "message 不能为空" });
  }

  // 建模型
  const { models, model } = buildModels(cfg);
  if (!model) {
    return sendJson(res, 400, {
      error: "AI 未配置：请先在设置里填写 API 地址、Key 和模型名",
    });
  }

  // 拉工具清单（Python 是真源）
  let schemas;
  try {
    schemas = await fetchToolSchemas(PYTHON_BASE, TOKEN);
  } catch (e) {
    return sendJson(res, 502, { error: `无法获取工具清单: ${e.message}` });
  }

  // 切到 SSE
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });

  const emitter = makeEmitter(res);
  emitter.raw("start", { protocol: "pi", mode, engine: "pi-agent-core" });

  const tools = buildAgentTools(schemas, PYTHON_BASE, TOKEN);

  const systemParts = [
    "你是 eNSP 网络实训课堂里的 Agent，精通华为 VRP 与 eNSP 模拟器。",
    "",
    "工作方式：",
    "1. 先查证再断言。凡涉及设备真实状态（支持什么协议、配了什么、通不通、",
    "   路径怎么走），必须先调用工具拿实测数据。",
    "2. 工具返回 ok=false 时读 error 字段判断原因；参数错就改参数重试，",
    "   设备不可达就如实告诉用户，不要假装成功。",
    "3. 拿到足够信息后立即给最终答案。",
    "",
    "关于下发（安全红线）：你没有任何直接下发配置的工具。你能做的最重的事是",
    "propose_config，它只产出一份待确认方案。产出后必须告诉用户去界面上核对并",
    "点确认，绝不要声称你已经把配置刷进去了。",
  ];
  if (mode === "engineer") {
    systemParts.push("", "当前为工程师模式：直接给出准确完整的命令序列。");
  } else {
    systemParts.push("", "当前为助教模式：引导式教学，先讲思路再给方向，不要一次给完答案。");
  }
  if (device_context) {
    systemParts.push("", "===== 学生设备实测上下文 =====", device_context);
  }

  const agent = new Agent({
    initialState: {
      systemPrompt: systemParts.join("\n"),
      model,
      tools,
    },
    streamFn: models.streamSimple.bind(models),
  });

  let steps = 0;
  agent.subscribe((event) => {
    if (event.type === "tool_execution_start") steps += 1;
    emitter.push(event);
  });

  try {
    const prior = [];
    for (const h of (history || []).slice(-12)) {
      if (h && (h.role === "user" || h.role === "assistant") && h.content) {
        prior.push({ role: h.role, content: String(h.content).slice(0, 4000) });
      }
    }
    if (prior.length) {
      // pi 的 prompt 只接受一条；历史以额外 user 上下文形式补进去
      const histText = prior
        .map((m) => `${m.role === "user" ? "用户" : "助手"}：${m.content}`)
        .join("\n");
      await agent.prompt(`【此前对话（供参考）】\n${histText}\n\n【本次请求】\n${message}`);
    } else {
      await agent.prompt(message);
    }

    // 收尾必推一次完整文本：节流可能把最后一段增量留在缓冲里
    if (emitter.state.text) {
      emitter.raw("answer", { answer: emitter.state.text, streaming: false });
    }
    emitter.raw("done", {
      steps: emitter.state.toolCalls.length,
      tool_calls: emitter.state.toolCalls,
    });
  } catch (e) {
    console.error("[sidecar] run failed:", e?.stack || e?.message || String(e));
    emitter.raw("error", { error: `Agent 执行失败: ${e?.message || String(e)}` });
    emitter.raw("done", { steps, tool_calls: emitter.state.toolCalls, failed: true });
  }
  res.end();
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`[sidecar] listening on 127.0.0.1:${PORT} pid=${process.pid}`);
});

// ---------------------------------------------------------------------------
// 父进程看护：Python 被强杀时 on_event("shutdown") 不会执行
// （Windows 上 Stop-Process -Force 走 TerminateProcess，没有 SIGTERM），
// 只靠信号处理会留下孤儿 sidecar 一直占着端口。这里轮询父进程是否还在，
// 不在就自己退出。PARENT_PID 由 Python 启动时通过环境变量传入。
// ---------------------------------------------------------------------------
const PARENT_PID = Number(process.env.AGENT_PARENT_PID || 0);

function parentAlive(pid) {
  try {
    // 信号 0 只做存在性检查，不真的发信号
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e?.code === "EPERM"; // 存在但无权限发信号 → 仍算活着
  }
}

if (PARENT_PID > 0) {
  const timer = setInterval(() => {
    if (!parentAlive(PARENT_PID)) {
      console.log(`[sidecar] 父进程 ${PARENT_PID} 已退出，自行关闭`);
      clearInterval(timer);
      server.close(() => process.exit(0));
      setTimeout(() => process.exit(0), 1000).unref();
    }
  }, 3000);
  timer.unref();
}

for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, () => {
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 2000);
  });
}
