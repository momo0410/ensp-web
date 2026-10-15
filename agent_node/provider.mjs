/**
 * provider.mjs - 把本项目 ai_config.json 里的模型接进 pi
 * =====================================================
 * 项目约定用 **OpenAI 兼容协议**接入任意模型服务（GLM / DeepSeek / Qwen /
 * OpenAI / 本地 Ollama）。pi 的 openAICompletionsApi() 正好是这条协议，
 * 所以这里只需把 (api_base, api_key, model) 组装成一个 pi provider。
 *
 * 踩过的坑（spike 实测，缺一个就会在 API 层崩）：
 * 1. auth.apiKey 必须是对象 { resolve }，不能是裸字符串；
 *    resolve 的返回值是 { auth: { apiKey }, source }，不是 { apiKey }。
 * 2. model 必须有 input / cost 字段，否则 openai-completions 内部
 *    `model.input.includes(...)` 会 undefined.includes 报错。
 * 3. baseUrl 由 provider 与 model 两处共同决定，两边都要给。
 */
import { createModels, createProvider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

/** 静态 API Key 的 auth 实现（pi 的 ProviderAuth 契约）。 */
function staticApiKeyAuth(key, label = "API key") {
  return {
    name: label,
    login: async () => ({ type: "api_key", key }),
    resolve: async () => ({ auth: { apiKey: key }, source: "ai_config.json" }),
  };
}

/** 规范化 base url：去掉结尾斜杠与已带的 /chat/completions 后缀。 */
function normalizeBase(base) {
  let b = String(base || "").trim().replace(/\/+$/, "");
  if (b.endsWith("/chat/completions")) {
    b = b.slice(0, -"/chat/completions".length);
  }
  return b;
}

/**
 * 依 ai_config.json 建一个 pi Models 实例。
 * 返回 { models, model }；model 为 null 表示配置不完整。
 */
export function buildModels(cfg) {
  const apiBase = normalizeBase(cfg.api_base);
  const apiKey = String(cfg.api_key || "");
  const modelId = String(cfg.model || "").trim();

  const models = createModels();
  if (!apiBase || !apiKey || !modelId) {
    return { models, model: null };
  }

  const provider = createProvider({
    id: "ensp-web",
    name: "eNSP Web 模型服务",
    baseUrl: apiBase,
    auth: { apiKey: staticApiKeyAuth(apiKey) },
    api: openAICompletionsApi(),
    models: [
      {
        id: modelId,
        name: modelId,
        provider: "ensp-web",
        api: "openai-completions",
        baseUrl: apiBase,
        // 必需字段：缺失会让 openai-completions 抛 undefined.includes
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: Number(cfg.context_window) || 128000,
        maxTokens: Number(cfg.max_tokens) || 8192,
        // 通用 OpenAI 兼容端点：关掉 OpenAI 专有字段
        compat: {
          supportsStore: false,
          supportsDeveloperRole: false,
          maxTokensField: "max_tokens",
        },
      },
    ],
  });

  models.setProvider(provider);
  const model = models.getModel("ensp-web", modelId) ?? null;
  return { models, model };
}
