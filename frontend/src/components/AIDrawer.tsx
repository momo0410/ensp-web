import React, { useEffect, useRef, useState } from "react"
import {
  Badge, Button, Drawer, Group, Loader, PasswordInput, Progress, Select, Stack, Tabs, Text, Textarea, TextInput,
} from "@mantine/core"
import { IconSend } from "@tabler/icons-react"
import { AI_QUICK_TASKS } from "../constants"
import { useApp } from "../store"
import { api, streamSSE } from "../api"
import { localFallback } from "../localFallback"
import { AiMarkdown } from "../AiMarkdown"
import type { ChatMessage, DevicePingResult, LabExperiment, LabProgress, AIStatus } from "../types"

const HISTORY_KEY = "ai_history"
const LAB_KEY = "ai_lab_session"

export function AIDrawer() {
  const {
    devices, aiDrawerOpen, setAiDrawerOpen, aiConfigured, setAiConfigured,
    capabilityResults, roleResults, lastDeploy, showStatus, aiBus, devicePing,
    setPendingFill,
  } = useApp()

  const [tab, setTab] = useState("chat")
  const [deviceIp, setDeviceIp] = useState("")
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState("")
  const [streaming, setStreaming] = useState(false)
  const aiHistoryRef = useRef<Array<{ role: string; content: string }>>([])

  // 实验指导
  const [experiments, setExperiments] = useState<LabExperiment[]>([])
  const [labSession, setLabSession] = useState<{ id: string } | null>(null)
  const [labProgress, setLabProgress] = useState<LabProgress | null>(null)
  const [labMessages, setLabMessages] = useState<ChatMessage[]>([])
  const [labInput, setLabInput] = useState("")

  // 设备侧 ping
  const [pingSrc, setPingSrc] = useState("")
  const [pingDst, setPingDst] = useState("")
  const [pingResult, setPingResult] = useState<Partial<DevicePingResult> & { invalid?: string; loading?: string } | null>(null)
  const [pingLoading, setPingLoading] = useState(false)
  const pingResultRef = useRef<Partial<DevicePingResult> | null>(null)

  // 设置
  const [aiStatus, setAiStatus] = useState<AIStatus | null>(null)
  const [setBase, setSetBase] = useState("")
  const [setKey, setSetKey] = useState("")
  const [setModel, setSetModel] = useState("")
  const [setMode, setSetMode] = useState("tutor")
  const [testResult, setTestResult] = useState("")
  const [savingSettings, setSavingSettings] = useState(false)

  const chatBoxRef = useRef<HTMLDivElement | null>(null)
  const labBoxRef = useRef<HTMLDivElement | null>(null)

  // 启动恢复对话历史（最近 8 条）
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]")
      if (Array.isArray(saved) && saved.length) {
        aiHistoryRef.current = saved
        setMessages(saved.slice(-8).map((m: { role: string; content: string }) => ({ role: m.role === "user" ? "user" : "assistant", content: m.content, done: true } as ChatMessage)))
      }
    } catch (e) { console.warn("AI 历史恢复失败", e) }
  }, [])

  useEffect(() => {
    if (!aiDrawerOpen) return
    loadAIStatus()
    if (!experiments.length) loadLabExperiments()
    restoreLabSession()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiDrawerOpen])

  useEffect(() => {
    chatBoxRef.current?.scrollTo({ top: chatBoxRef.current.scrollHeight })
  }, [messages])
  useEffect(() => {
    labBoxRef.current?.scrollTo({ top: labBoxRef.current.scrollHeight })
  }, [labMessages])

  const saveHistory = () => {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(aiHistoryRef.current.slice(-40))) } catch (e) { /* ignore */ }
  }

  // ---------- 对话 ----------
  const sendAIChatRef = useRef<((task?: string, messageText?: string) => Promise<void>) | null>(null)
  sendAIChatRef.current = async (task?: string, messageText?: string) => {
    if (streaming) return
    const message = (messageText !== undefined ? messageText : input).trim()
    if (!message) return
    if (messageText === undefined) setInput("")

    const userId = `u${Date.now()}`
    const botId = `b${Date.now()}`
    setMessages((prev) => [...prev,
      { id: userId, role: "user", content: message, done: true },
      { id: botId, role: "assistant", content: "…", done: false },
    ])
    aiHistoryRef.current.push({ role: "user", content: message })
    setStreaming(true)
    try {
      const reply = await streamSSE("/api/ai/chat", {
        message, task: task || "free", device_ip: deviceIp || null,
        history: aiHistoryRef.current.slice(0, -1), stream: true,
      }, (text) => {
        setMessages((prev) => prev.map((m) => (m.id === botId ? { ...m, content: text } : m)))
      })
      setMessages((prev) => prev.map((m) => (m.id === botId ? { ...m, content: reply || "（空回复）", done: true } : m)))
      aiHistoryRef.current.push({ role: "assistant", content: reply })
      saveHistory()
    } catch (e) {
      setMessages((prev) => prev.map((m) => (m.id === botId ? { ...m, error: (e as Error).message, done: true } : m)))
    } finally {
      setStreaming(false)
    }
  }

  const quickTaskRef = useRef<((task: string) => void) | null>(null)
  quickTaskRef.current = (task: string) => {
    if (!deviceIp) {
      showStatus("warning", "请先在 AI 助教顶部选择一台设备")
      return
    }
    if (aiConfigured === false) {
      const text = localFallback(task, deviceIp, {
        capResult: capabilityResults[deviceIp],
        roleResult: roleResults[deviceIp],
        lastDeploy,
      })
      if (text) {
        const fallbackText = text
          + "\n\n> 以上为**本地确定性解读**（未接入 AI 服务）。在\"设置\"里填入 API 地址 / Key / 模型名后，可获得更深入的讲解。"
        setMessages((prev) => [...prev,
          { id: `u${Date.now()}`, role: "user", content: (AI_QUICK_TASKS as Record<string, string>)[task] || task, done: true },
          { id: `b${Date.now()}`, role: "assistant", content: fallbackText, done: true },
        ])
        aiHistoryRef.current.push({ role: "assistant", content: text })
        saveHistory()
        return
      }
    }
    if (task === "diagnose") {
      const last = lastDeploy
      if (!last || !last.error) {
        sendAIChatRef.current?.("diagnose", "这台设备最近一次配置下发没有记录到错误。如果设备有异常，请把报错原文发给我。")
        return
      }
      const msg = `请诊断这台设备最近一次配置下发失败的原因。报错信息：\n${last.error}`
        + (last.outputs ? `\n执行回显：\n${last.outputs}` : "")
      sendAIChatRef.current?.("diagnose", msg)
      return
    }
    sendAIChatRef.current?.(task, (AI_QUICK_TASKS as Record<string, string>)[task])
  }

  const clearHistory = () => {
    aiHistoryRef.current = []
    localStorage.removeItem(HISTORY_KEY)
    setMessages([{ role: "assistant", content: "对话已清空。可以继续提问，或选择设备使用上方快捷动作。", done: true }])
  }

  // ---------- AI 设置 ----------
  const loadAIStatus = async () => {
    try {
      const data = await api("/api/ai/status")
      setAiStatus(data)
      setAiConfigured(!!data.configured)
      if (data.api_base) setSetBase((prev) => prev || data.api_base)
      if (data.model) setSetModel((prev) => prev || data.model)
      setSetMode(data.mode || "tutor")
    } catch (e) {
      console.error("AI 状态加载失败:", (e as Error).message)
    }
  }

  const saveAISettings = async () => {
    setSavingSettings(true)
    try {
      const data = await api("/api/ai/config", {
        method: "POST",
        body: JSON.stringify({
          api_base: setBase.trim(), api_key: setKey.trim(),
          model: setModel.trim(), mode: setMode,
        }),
      })
      setSetKey("")
      setTestResult(data.configured ? "✔ 配置已保存" : "已保存，但信息不完整，暂无法对话")
      loadAIStatus()
    } catch (e) {
      setTestResult("保存失败: " + (e as Error).message)
    } finally {
      setSavingSettings(false)
    }
  }

  const testAIConnection = async () => {
    setTestResult("正在测试连接…")
    try {
      const data = await api("/api/ai/test", { method: "POST" })
      setTestResult((data.success ? "✔ 连接成功 " : "✘ 失败 ") + (data.message || ""))
    } catch (e) {
      setTestResult("✘ 失败 " + (e as Error).message)
    }
  }

  // ---------- 实验指导 ----------
  const loadLabExperiments = async () => {
    try {
      const data = await api("/api/ai/lab/experiments")
      setExperiments(data.experiments || [])
    } catch (e) {
      console.error("实验目录加载失败:", (e as Error).message)
    }
  }

  const startLab = async (experimentId: string) => {
    try {
      const data = await api("/api/ai/lab/start", {
        method: "POST",
        body: JSON.stringify({ experiment_id: experimentId, device_ips: [] }),
      })
      setLabSession(data.session)
      localStorage.setItem(LAB_KEY, data.session.id)
      setLabProgress(data.progress)
      setLabMessages([{
        role: "assistant", done: true,
        content: `实验「${data.progress.experiment_name}」已开始。目标：${data.progress.goal}。\n配置过程中随时问我；完成或卡住时点"重新校验"查看进度。`,
      }])
    } catch (e) {
      showStatus("error", `开始实验失败: ${(e as Error).message}`)
    }
  }

  const exitLab = () => {
    setLabSession(null)
    setLabProgress(null)
    localStorage.removeItem(LAB_KEY)
  }

  const refreshLabProgress = async () => {
    if (!labSession) return
    try {
      const data = await api(`/api/ai/lab/${labSession.id}/progress`)
      setLabProgress(data.progress)
      setLabMessages((prev) => [...prev, {
        role: "assistant", done: true,
        content: `进度已刷新：${data.progress.progress}%。`
          + (data.progress.progress === 100 ? " 全部校验通过，实验完成！" : " 未完成的项见上方 ✗ 标记，可继续问我。"),
      }])
    } catch (e) {
      showStatus("error", `刷新进度失败: ${(e as Error).message}`)
    }
  }

  const restoreLabSession = async () => {
    const saved = localStorage.getItem(LAB_KEY)
    if (!saved || labSession) return
    try {
      const data = await api(`/api/ai/lab/${saved}/progress`)
      setLabSession({ id: saved })
      setLabProgress(data.progress)
      setLabMessages([{
        role: "assistant", done: true,
        content: "已恢复上次的实验会话，进度如上。继续配置后可点\"重新校验\"。",
      }])
    } catch (e) {
      localStorage.removeItem(LAB_KEY)
    }
  }

  const askLabHintRef = useRef<((question?: string) => Promise<void>) | null>(null)
  askLabHintRef.current = async (questionOverride?: string) => {
    if (!labSession || streaming) return
    const question = (questionOverride !== undefined ? questionOverride : labInput).trim()
    if (!question) return
    if (questionOverride === undefined) setLabInput("")
    const botId = `lb${Date.now()}`
    setLabMessages((prev) => [...prev,
      { role: "user", content: question, done: true },
      { id: botId, role: "assistant", content: "…", done: false },
    ])
    setStreaming(true)
    try {
      await streamSSE(`/api/ai/lab/${labSession.id}/hint`, { question, stream: true }, (text) => {
        setLabMessages((prev) => prev.map((m) => (m.id === botId ? { ...m, content: text } : m)))
      })
      setLabMessages((prev) => prev.map((m) => (m.id === botId ? { ...m, done: true } : m)))
    } catch (e) {
      setLabMessages((prev) => prev.map((m) => (m.id === botId ? { ...m, error: (e as Error).message, done: true } : m)))
    } finally {
      setStreaming(false)
    }
  }

  // ---------- 设备侧 ping ----------
  const runDevicePing = async () => {
    if (!pingSrc || !pingDst) {
      setPingResult({ invalid: "请先选择源设备和目标 IP" })
      return
    }
    setPingLoading(true)
    setPingResult({ loading: `正在从 ${pingSrc} ping ${pingDst} …（约 5-10 秒）` })
    try {
      const data = await devicePing(pingSrc, pingDst, 4)
      pingResultRef.current = data
      setPingResult(data)
    } catch (e) {
      setPingResult({ error: (e as Error).message })
    } finally {
      setPingLoading(false)
    }
  }

  const pingToTutor = () => {
    const p = pingResultRef.current
    if (!p) return
    const summary = `我刚在设备 ${pingSrc} 上执行了 \`${p.command}\`：`
      + ` 发包 ${p.transmitted}，收包 ${p.received}，`
      + `丢包率 ${p.loss_rate != null ? p.loss_rate + "%" : "未知"}，`
      + `平均时延 ${p.avg_ms != null ? p.avg_ms + "ms" : "未知"}。`
      + (p.success ? "结果是通的。" : "结果不通。")
      + "请结合我的实验目标分析这个结果说明了什么"
      + (p.success ? "" : "，并提示我可能的排查方向")
    if (labSession) askLabHintRef.current?.(summary)
    else sendAIChatRef.current?.("diagnose", summary)
  }

  // ---------- 对外总线：诊断入口 / 模板跳转 ----------
  const openDiagnoseRef = useRef<(() => void) | null>(null)
  openDiagnoseRef.current = () => {
    setAiDrawerOpen(true)
    setTab("chat")
    const last = lastDeploy
    if (!last || !last.error) return
    const ip = last.ip || deviceIp
    if (ip && devices.some((d) => d.ip === ip)) setDeviceIp(ip)
    const msg = `请诊断这台设备最近一次配置下发失败的原因。报错信息：\n${last.error}`
      + (last.outputs ? `\n执行回显：\n${last.outputs}` : "")
    setTimeout(() => sendAIChatRef.current?.("diagnose", msg), 300)
  }

  const templateJumpRef = useRef<((key: string) => void) | null>(null)
  templateJumpRef.current = (templateKey: string) => {
    const ip = deviceIp
    if (!ip) {
      showStatus("warning", "请先在 AI 助教顶部选择设备，再打开填空模板")
      return
    }
    setAiDrawerOpen(false)
    setPendingFill({ ip, configKey: templateKey, protocolKey: "" })
  }

  useEffect(() => {
    aiBus.current.openDiagnose = () => openDiagnoseRef.current?.()
    aiBus.current.templateJump = (key: string) => templateJumpRef.current?.(key)
    return () => { delete aiBus.current.openDiagnose; delete aiBus.current.templateJump }
  }, [aiBus])

  return (
    <Drawer opened={aiDrawerOpen} onClose={() => setAiDrawerOpen(false)} position="right" size="560"
      title={<Group gap={8}><Text fw={600}>🤖 AI 助教</Text>
        <Badge variant="light" size="sm" color={aiStatus?.mode === "engineer" ? "teal" : "blue"}>
          {aiStatus ? (aiStatus.mode === "engineer" ? "工程师模式" : "助教模式") : "…"}
        </Badge></Group>}
      styles={{ body: { height: "calc(100% - 60px)", padding: 0, display: "flex", flexDirection: "column" } }}>
      <Tabs value={tab} onChange={(v) => setTab(v || "chat")} style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
        <Tabs.List px="md">
          <Tabs.Tab value="chat">对话</Tabs.Tab>
          <Tabs.Tab value="lab">实验指导</Tabs.Tab>
          <Tabs.Tab value="settings">设置</Tabs.Tab>
        </Tabs.List>

        {/* 对话 */}
        <Tabs.Panel value="chat" style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }} p="md">
          <Group gap="xs" mb="xs" wrap="nowrap">
            <Select flex={1} size="xs" searchable
              placeholder="-- 不关联设备（通用提问） --"
              data={devices.map((d) => ({ value: d.ip, label: `${d.name} (${d.ip})` }))}
              value={deviceIp} onChange={(v) => setDeviceIp(v || "")} />
          </Group>
          <Group gap={4} mb="xs">
            <Button size="compact-xs" variant="default" onClick={() => quickTaskRef.current?.("explain_capabilities")}>能力解读</Button>
            <Button size="compact-xs" variant="default" onClick={() => quickTaskRef.current?.("explain_roles")}>角色讲解</Button>
            <Button size="compact-xs" variant="default" onClick={() => quickTaskRef.current?.("explain_config")}>解释配置</Button>
            <Button size="compact-xs" variant="default" onClick={() => quickTaskRef.current?.("diagnose")}>诊断报错</Button>
            <Button size="compact-xs" variant="default" onClick={clearHistory}>清空</Button>
          </Group>

          <div ref={chatBoxRef} className="ai-msgs" style={{ flex: 1, minHeight: 0, padding: "4px 2px" }}>
            {!messages.length && (
              <div className="ai-msg assistant">
                你好！我是本课堂的 AI 助教。可以先在上方选择一台设备，我再结合它的实测数据回答。
                <br />快捷键：<b>能力解读</b> = 为什么支持/不支持这些协议；<b>角色讲解</b> = 这台设备在每个协议里扮演什么；
                <b>解释配置</b> = 逐段讲当前配置；<b>诊断报错</b> = 分析最近一次下发失败。
              </div>
            )}
            {messages.map((m) => (
              <div key={m.id || m.content} className={`ai-msg ${m.role}`}>
                {m.error
                  ? <span><Badge color="red" variant="light" size="sm">出错</Badge> {m.error}<br /><small>若是未配置，请点"设置"填入 API 地址、Key 与模型名。</small></span>
                  : m.role === "assistant" && m.done
                    ? <AiMarkdown text={m.content} onTemplate={(key) => templateJumpRef.current?.(key)} />
                    : m.content}
              </div>
            ))}
          </div>

          <Group align="flex-end" mt="xs" gap="xs" wrap="nowrap">
            <Textarea flex={1} minRows={2} maxRows={5} autosize
              placeholder="问点什么…（Enter 发送，Shift+Enter 换行）"
              value={input}
              onChange={(e) => setInput(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault()
                  sendAIChatRef.current?.()
                }
              }} />
            <Button disabled={streaming} onClick={() => sendAIChatRef.current?.()}
              leftSection={<IconSend size={14} />}>发送</Button>
          </Group>
        </Tabs.Panel>

        {/* 实验指导 */}
        <Tabs.Panel value="lab" style={{ flex: 1, minHeight: 0, overflowY: "auto" }} p="md">
          {!labSession ? (
            <Stack gap="sm">
              <Text size="sm" c="dimmed">
                选择一个实验开始。进度校验基于"能力识别"的实测数据，AI 助教会按进度给引导提示（不会直接给整段答案）。
              </Text>
              {experiments.map((e) => (
                <Stack key={e.id} gap={4} p="sm" style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8 }}>
                  <Group justify="space-between">
                    <Text size="sm" fw={600}>{e.name}</Text>
                    <Button size="xs" onClick={() => startLab(e.id)}>开始</Button>
                  </Group>
                  <Text size="xs">{e.goal}</Text>
                  <Text size="xs" c="dimmed">{e.check_count} 项校验 · 适用: {(e.applicable_roles || []).join("/")}</Text>
                </Stack>
              ))}
              {experiments.length === 0 && <Text size="sm" c="dimmed">正在加载实验目录…</Text>}
            </Stack>
          ) : (
            <Stack gap="sm">
              <Group justify="space-between">
                <div>
                  <Text size="sm" fw={600}>{labProgress?.experiment_name}</Text>
                  <Text size="xs" c="dimmed">{labProgress?.goal}</Text>
                </div>
                <Button size="xs" variant="default" onClick={exitLab}>退出</Button>
              </Group>
              <Progress value={labProgress?.progress || 0} />
              <Stack gap={4}>
                {(labProgress?.checks || []).map((c, i) => (
                  <Group key={i} justify="space-between" gap="xs">
                    <Text size="xs" c={c.passed ? "teal" : "red"}>{c.passed ? "✓" : "✗"} {c.desc}</Text>
                    <Text size="xs" c="dimmed">{c.passed_devices.length}/{c.required} 台: {c.passed_devices.join(", ") || "—"}</Text>
                  </Group>
                ))}
              </Stack>
              <Group>
                <Button size="xs" variant="default" onClick={refreshLabProgress}>↻ 重新校验（基于最近能力识别）</Button>
              </Group>

              <div ref={labBoxRef} className="ai-msgs" style={{ maxHeight: 220, padding: "4px 2px" }}>
                {labMessages.map((m, i) => (
                  <div key={i} className={`ai-msg ${m.role}`}>
                    {m.error
                      ? <span><Badge color="red" variant="light" size="sm">出错</Badge> {m.error}</span>
                      : m.role === "assistant" && m.done ? <AiMarkdown text={m.content} /> : m.content}
                  </div>
                ))}
              </div>
              <Group align="flex-end" gap="xs" wrap="nowrap">
                <Textarea flex={1} minRows={2} maxRows={4} autosize
                  placeholder="卡住了？告诉助教你卡在哪…（Enter 发送）"
                  value={labInput}
                  onChange={(e) => setLabInput(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault()
                      askLabHintRef.current?.()
                    }
                  }} />
                <Button disabled={streaming} onClick={() => askLabHintRef.current?.()}>问助教</Button>
              </Group>
            </Stack>
          )}

          {/* 连通性验证（设备侧 ping） */}
          <Stack gap="xs" mt="lg" p="sm" style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8 }}>
            <Text size="sm" fw={600}>连通性验证（设备侧 ping）</Text>
            <Group gap="xs" wrap="nowrap">
              <Select flex={1} size="xs" placeholder="-- 源设备 --" searchable
                data={devices.map((d) => ({ value: d.ip, label: `${d.name} (${d.ip})` }))}
                value={pingSrc} onChange={(v) => setPingSrc(v || "")} />
              <Text size="sm">→</Text>
              <Select flex={1} size="xs" placeholder="-- 目标 IP --" searchable
                data={devices.map((d) => ({ value: d.ip, label: d.ip }))}
                value={pingDst} onChange={(v) => setPingDst(v || "")} />
              <Button size="xs" loading={pingLoading} onClick={runDevicePing}>测试</Button>
            </Group>
            {pingResult && (
              <div>
                {pingResult.invalid && <Text size="sm" c="red">{pingResult.invalid}</Text>}
                {pingResult.loading && <Group gap={6}><Loader size={12} /><Text size="sm" c="dimmed">{pingResult.loading}</Text></Group>}
                {pingResult.error && <Text size="sm" c="red">✘ 失败 {pingResult.error}</Text>}
                {pingResult.command && (
                  <>
                    <Group gap={6}>
                      <Badge color={pingResult.success ? "teal" : "red"} variant="light" size="sm">
                        {pingResult.success ? "连通" : "不通"}
                      </Badge>
                      <Text size="sm">
                        丢包 {pingResult.loss_rate != null ? pingResult.loss_rate + "%" : "?"} ·
                        平均 {pingResult.avg_ms != null ? pingResult.avg_ms + "ms" : "?"
                          + (pingResult.error ? ` · ${pingResult.error}` : "")}
                      </Text>
                    </Group>
                    {pingResult.error && <Text size="sm" c="dimmed">{pingResult.error}</Text>}
                    <pre className="mono-pre" style={{ maxHeight: 140 }}>{pingResult.raw_output || ""}</pre>
                    <Button size="xs" variant="default" mt={4} onClick={pingToTutor}>🤖 把结果告诉助教分析</Button>
                  </>
                )}
              </div>
            )}
          </Stack>
        </Tabs.Panel>

        {/* 设置 */}
        <Tabs.Panel value="settings" style={{ flex: 1, minHeight: 0, overflowY: "auto" }} p="md">
          <Stack gap="sm">
            {aiStatus?.configured ? (
              <Text size="sm" c="teal">
                已接入 {aiStatus.model}（Key: {aiStatus.key_masked}），模式: {aiStatus.mode === "engineer" ? "工程师" : "助教"}
              </Text>
            ) : (
              <Text size="sm" c="yellow">未配置：请填写 API 地址、Key 和模型名（任意 OpenAI 兼容服务均可）</Text>
            )}
            <TextInput label="API 地址（OpenAI 兼容基址）"
              placeholder="https://open.bigmodel.cn/api/paas/v4 或 https://api.deepseek.com/v1"
              value={setBase} onChange={(e) => setSetBase(e.currentTarget.value)} />
            <PasswordInput label="API Key" placeholder="留空则保留已保存的 Key"
              value={setKey} onChange={(e) => setSetKey(e.currentTarget.value)} />
            <TextInput label="模型名"
              placeholder="glm-4-flash / deepseek-chat / qwen-plus / 本地 Ollama 模型名…"
              value={setModel} onChange={(e) => setSetModel(e.currentTarget.value)} />
            <Select label="助教模式" value={setMode} onChange={(v) => setSetMode(v || "tutor")}
              data={[
                { value: "tutor", label: "助教模式（引导式教学，推荐课堂使用）" },
                { value: "engineer", label: "工程师模式（直接给出完整命令）" },
              ]} />
            <Group>
              <Button loading={savingSettings} onClick={saveAISettings}>保存配置</Button>
              <Button variant="default" onClick={testAIConnection}>测试连接</Button>
            </Group>
            {testResult && <Text size="sm">{testResult}</Text>}
            <Text size="xs" c="dimmed">
              Key 仅保存在本机 ai_config.json（已加入 .gitignore，不会提交）；对话请求由本机直接发往你配置的服务商。
            </Text>
          </Stack>
        </Tabs.Panel>
      </Tabs>
    </Drawer>
  )
}
