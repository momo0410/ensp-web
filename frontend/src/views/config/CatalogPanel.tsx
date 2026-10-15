import React, { useEffect, useRef, useState } from "react"
import {
  Badge, Button, Checkbox, Group, Paper, Select, Stack, NumberInput, Text, Title,
} from "@mantine/core"
import { notifications } from "@mantine/notifications"
import { DEVICE_TYPE_LABELS } from "../../constants"
import { useApp } from "../../store"
import { api } from "../../api"
import { useConfirm, MonoPre, decodeEntities } from "../../ui"
import { collectParams, FieldInput } from "../../components/FieldInput"
import { DeployResult } from "../../components/DeployResult"
import type { CatalogEntry, ProbeResult } from "../../types"

// 命令目录：每条指令 = 一份表单定义；? 探测标注支持/不支持
export function CatalogPanel() {
  const { devices } = useApp()
  const confirm = useConfirm()

  const [categories, setCategories] = useState<string[]>([])
  const [entries, setEntries] = useState<CatalogEntry[]>([])
  const [loadError, setLoadError] = useState("")
  const [probe, setProbe] = useState<ProbeResult | null>(null) // ? 探测结果
  const [probeIp, setProbeIp] = useState("")
  const [probeForce, setProbeForce] = useState(false)
  const [onlySupported, setOnlySupported] = useState(false)
  const [probing, setProbing] = useState(false)
  const [probeStatus, setProbeStatus] = useState<{ kind: string; text: string } | null>(null)

  // 表单态
  const [currentKey, setCurrentKey] = useState<string | null>(null)
  const [params, setParams] = useState({})
  const [targetIp, setTargetIp] = useState("")
  const [saveConfig, setSaveConfig] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  const [result, setResult] = useState<Record<string, any> | null>(null)
  const [deploying, setDeploying] = useState(false)

  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  useEffect(() => () => { if (pollTimer.current) clearInterval(pollTimer.current) }, [])

  useEffect(() => {
    ;(async () => {
      try {
        const data = await api("/api/command-catalog")
        setCategories(data.categories || [])
        setEntries(data.entries || [])
      } catch (e) {
        setLoadError("加载命令目录失败: " + (e as Error).message)
      }
    })()
  }, [])

  const applyProbeResult = (result: ProbeResult, fromCache: boolean) => {
    setProbe(result)
    const dt = (DEVICE_TYPE_LABELS as Record<string, string>)[result.device_type || ""] || result.device_type || "未知"
    const cacheTag = fromCache ? "（缓存命中，秒回）" : ""
    const errTag = result.errors?.length ? ` ⚠ ${result.errors.join("; ")}` : ""
    setProbeStatus({
      kind: "done",
      text: `已探测 ${dt}${result.model ? " · " + result.model : ""}：支持 ${result.supported.length} / 不支持 ${result.unsupported.length}${cacheTag}${errTag}`,
    })
    setProbing(false)
  }

  const doProbe = async () => {
    const dev = devices.find((d) => d.ip === probeIp)
    if (!dev) { notifications.show({ color: "red", message: "请选择目标设备" }); return }
    const target = { ip: dev.ip, console_port: dev.console_port ?? undefined }
    setProbing(true)
    setProbeStatus({ kind: "probing", text: "探测中…" })
    try {
      const data = await api("/api/command-catalog/probe", {
        method: "POST",
        body: JSON.stringify({ target, force: probeForce }),
      })
      if (data.status === "done") {
        applyProbeResult(data.result, data.from_cache)
      } else if (data.status === "probing") {
        // 后台任务：800ms 轮询，UI 不阻塞
        if (pollTimer.current) clearInterval(pollTimer.current)
        pollTimer.current = setInterval(async () => {
          try {
            const d = await api(`/api/command-catalog/probe/${data.job_id}`)
            if (d.status === "done") {
              if (pollTimer.current) clearInterval(pollTimer.current)
              pollTimer.current = null
              applyProbeResult(d.result, false)
            } else if (d.status === "error") {
              if (pollTimer.current) clearInterval(pollTimer.current)
              pollTimer.current = null
              setProbeStatus({ kind: "error", text: "探测失败: " + (d.error || "未知错误") })
              setProbing(false)
            }
          } catch (e) { /* 轮询异常，等下一轮 */ }
        }, 800)
      }
    } catch (e) {
      setProbeStatus({ kind: "error", text: "探测失败: " + (e as Error).message })
      setProbing(false)
    }
  }

  const supportedSet = new Set(probe ? probe.supported : [])
  const unsupportedSet = new Set(probe ? probe.unsupported : [])
  let viewEntries = entries
  if (onlySupported && probe) viewEntries = entries.filter((e) => supportedSet.has(e.key))

  const currentEntry = currentKey ? entries.find((e) => e.key === currentKey) : null

  const doPreview = async () => {
    if (!currentKey) return
    try {
      const data = await api("/api/command-catalog/render", {
        method: "POST",
        body: JSON.stringify({ key: currentKey, params }),
      })
      setPreview((data.commands || []).join("\n"))
    } catch (e) {
      notifications.show({ color: "red", message: "预览失败: " + (e as Error).message })
    }
  }

  const doDeploy = async () => {
    if (!currentKey) return
    const body: Record<string, unknown> = { key: currentKey, params: collectParams(currentEntry!, params), save_config: saveConfig }
    if (!targetIp) { notifications.show({ color: "red", message: "请选择目标设备" }); return }
    body.target_ip = targetIp
    const ok = await confirm("确认下发配置",
      "即将通过本地 Console 通道下发指令，是否继续？")
    if (!ok) return
    setDeploying(true)
    try {
      const data = await api("/api/command-catalog/deploy", { method: "POST", body: JSON.stringify(body) })
      setResult(data)
    } catch (e) {
      setResult({ success: false, error: (e as Error).message })
    } finally {
      setDeploying(false)
    }
  }

  const cardFor = (e: CatalogEntry) => {
    const supported = probe && supportedSet.has(e.key)
    const unsupported = probe && unsupportedSet.has(e.key)
    return (
      <button key={e.key} className={`catalog-card${unsupported ? " dim" : ""}`} onClick={() => openForm(e)}>
        <span>{decodeEntities(e.icon) || "⚙"}</span>
        <span>{e.name}</span>
        {probe && supported && <Badge color="teal" variant="light" size="sm" title="设备支持">✔</Badge>}
        {probe && unsupported && <Badge color="red" variant="light" size="sm" title="设备不支持">✖</Badge>}
      </button>
    )
  }

  const openForm = (e: CatalogEntry) => {
    setCurrentKey(e.key)
    setParams({})
    setPreview(null)
    setResult(null)
  }

  return (
    <Paper withBorder p="md" radius="md">
      <Title order={5} mb={4}>命令目录（点表单即可配置，无需写命令）</Title>
      <Text size="xs" c="dimmed" mb="sm">每条指令 = 一份表单定义；加新指令只改数据文件，不改代码</Text>

      <Stack gap="sm">
        <Group align="center" gap="xs" wrap="wrap">
          <Text size="sm">? 探测目标：</Text>
          <Select w={220} size="xs" placeholder="-- 选择设备 --" searchable
            data={devices.map((d) => ({ value: d.ip, label: `${d.name} (Console ${d.console_port ?? "-"})` }))}
            value={probeIp} onChange={(v) => setProbeIp(v || "")} />
          <Button size="xs" variant="default" loading={probing} onClick={doProbe}>探测支持命令</Button>
          <Checkbox size="xs" label="忽略缓存" checked={probeForce} onChange={(e) => setProbeForce(e.currentTarget.checked)} />
          <Checkbox size="xs" label="仅显示支持" checked={onlySupported}
            onChange={(e) => setOnlySupported(e.currentTarget.checked)} />
          {probeStatus && (
            <Text size="xs" c={probeStatus.kind === "error" ? "red" : probeStatus.kind === "probing" ? "dimmed" : "teal"}>
              {probeStatus.text}
            </Text>
          )}
        </Group>

        {loadError && <Text size="sm" c="red">{loadError}</Text>}

        {!currentEntry && (
          <Stack gap="sm">
            {viewEntries.length === 0 && !loadError && (
              <Text size="sm" c="dimmed">
                {entries.length ? "当前设备不支持任何已探测指令（或未探测）" : "命令目录为空"}
              </Text>
            )}
            {categories.filter((cat) => viewEntries.some((e) => e.category === cat)).map((cat) => (
              <div key={cat}>
                <Text size="sm" fw={600} mb={6}>{cat}</Text>
                <div className="catalog-grid">
                  {viewEntries.filter((e) => e.category === cat).map(cardFor)}
                </div>
              </div>
            ))}
            {viewEntries.some((e) => !categories.includes(e.category)) && (
              <div className="catalog-grid">
                {viewEntries.filter((e) => !categories.includes(e.category)).map(cardFor)}
              </div>
            )}
          </Stack>
        )}

        {currentEntry && (
          <Stack gap="sm">
            <Group justify="space-between">
              <Text fw={600} size="sm">{currentEntry.name}</Text>
              <Button size="xs" variant="default" onClick={() => { setCurrentKey(null); setPreview(null); setResult(null) }}>
                ← 返回目录
              </Button>
            </Group>

            <Stack gap="sm">
              {(currentEntry.fields || []).map((field) => (
                <FieldInput key={field.name} field={field} params={params} setParams={setParams} interfaces={null} />
              ))}
            </Stack>

            <Group align="center" gap="xs">
              <Text size="sm">下发目标：</Text>
              <Select w={240} size="xs" placeholder="-- 请选择设备 --" searchable
                data={devices.map((d) => ({ value: d.ip, label: `${d.name} (Console ${d.console_port ?? "-"})` }))}
                value={targetIp} onChange={(v) => setTargetIp(v || "")} />
            </Group>

            <Group gap="md">
              <Button variant="default" size="xs" onClick={doPreview}>预览</Button>
              <Button size="xs" loading={deploying} onClick={doDeploy}>下发配置</Button>
              <Checkbox size="xs" label="下发后保存配置" checked={saveConfig}
                onChange={(e) => setSaveConfig(e.currentTarget.checked)} />
            </Group>

            {preview !== null && (
              <div>
                <Text size="sm" fw={500} mb={4}>命令预览</Text>
                <MonoPre>{preview}</MonoPre>
              </div>
            )}
            {result && <DeployResult data={result} ip={targetIp || null} />}
          </Stack>
        )}
      </Stack>
    </Paper>
  )
}
