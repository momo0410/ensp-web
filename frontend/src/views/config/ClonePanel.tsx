import React, { useMemo, useState } from "react"
import { Button, Checkbox, Group, Paper, Stack, Text, Title } from "@mantine/core"
import { notifications } from "@mantine/notifications"
import { IconCopy, IconSend } from "@tabler/icons-react"
import { ROLE_NAMES } from "../../constants"
import { useApp } from "../../store"
import type { CloneDeployResult, ClonePreview } from "../../types"
import { api } from "../../api"
import { useConfirm, MonoPre } from "../../ui"

const CLONE_DOMAINS = [
  ["identity", "设备身份"], ["basic", "基础功能"], ["interfaces", "接口配置"],
  ["switching", "交换配置"], ["routing", "路由协议"], ["services", "网络服务/VPN"],
]

// 一键复制当前设备配置到其他设备（自动适配冲突）
export function ClonePanel({ selectedIp }: { selectedIp: string }) {
  const { devices, capabilityResults } = useApp()
  const confirm = useConfirm()

  const [targets, setTargets] = useState<string[]>([])
  const [domains, setDomains] = useState(CLONE_DOMAINS.map(([v]) => v))
  const [autoResolve, setAutoResolve] = useState(true)
  const [saveConfig, setSaveConfig] = useState(false)
  const [preview, setPreview] = useState<ClonePreview | null>(null) // clone/preview 响应
  const [result, setResult] = useState<CloneDeployResult | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [deploying, setDeploying] = useState(false)

  const source = devices.find((d) => d.ip === selectedIp)
  const targetDevices = useMemo(() => devices.filter((d) => d.ip !== selectedIp), [devices, selectedIp])
  const blocking = !preview?.success || (preview?.conflicts?.errors || []).length > 0

  const doPreview = async () => {
    if (!selectedIp) { notifications.show({ color: "red", message: "请先选择源设备" }); return }
    if (!targets.length) { notifications.show({ color: "red", message: "请至少选择一台目标设备" }); return }
    if (!domains.length) { notifications.show({ color: "red", message: "请至少选择一个复制范围" }); return }
    setPreviewing(true)
    setResult(null)
    try {
      const data = await api("/api/config/clone/preview", {
        method: "POST",
        body: JSON.stringify({
          source_device: selectedIp,
          target_devices: targets,
          domains,
          auto_resolve: autoResolve,
          include_sensitive: false,
          refresh_config: true,
        }),
      })
      setPreview(data)
    } catch (e) {
      setPreview(null)
      notifications.show({ color: "red", message: "生成复制方案失败: " + (e as Error).message })
    } finally {
      setPreviewing(false)
    }
  }

  const doDeploy = async () => {
    if (!preview?.preview_id) { notifications.show({ color: "red", message: "请先生成复制方案" }); return }
    const targetCount = Object.keys(preview.targets || {}).length
    const ok = await confirm("确认复制设备配置",
      `即将把源设备配置复制到 ${targetCount} 台目标设备。\n系统会再次备份目标设备，并仅下发预览中的命令。\n\n是否确认继续？`)
    if (!ok) return
    setDeploying(true)
    try {
      const data = await api("/api/config/clone/deploy", {
        method: "POST",
        body: JSON.stringify({ preview_id: preview.preview_id, confirm: true, save_config: saveConfig }),
      })
      setResult(data)
    } catch (e) {
      setResult({ success: false, error: (e as Error).message, results: {}, summary: {} })
    } finally {
      setDeploying(false)
    }
  }

  const conflicts = preview?.conflicts || {}
  const warnings = preview?.warnings || []

  return (
    <Paper withBorder p="md" radius="md">
      <Title order={5} mb={4}>一键复制当前设备配置到其他设备</Title>
      <Text size="xs" c="dimmed" mb="sm">自动适配设备名、Router ID、IS-IS NET 和冲突接口 IP；敏感管理配置默认隔离</Text>

      <Stack gap="sm">
        <Text size="sm">源设备：<b>{source ? `${source.name} (${source.ip})` : "请先在上方选择源设备"}</b></Text>

        <Group align="flex-start" gap="xl" wrap="nowrap">
          <Stack gap={4}>
            <Text size="sm" fw={500}>目标设备（可多选）</Text>
            {targetDevices.length === 0 && <Text size="xs" c="dimmed">没有可选目标设备</Text>}
            {targetDevices.map((d) => {
              const cap = capabilityResults[d.ip]
              const role = cap ? ((ROLE_NAMES as Record<string, string>)[cap.device_role || ""] || cap.device_role || "") : ""
              return (
                <Checkbox key={d.ip} size="xs"
                  label={`${d.name} (${d.ip})${role ? ` · ${role}` : ""}`}
                  checked={targets.includes(d.ip)}
                  onChange={(e) => setTargets(e.currentTarget.checked
                    ? [...targets, d.ip]
                    : targets.filter((ip) => ip !== d.ip))} />
              )
            })}
          </Stack>
          <Stack gap={4}>
            <Text size="sm" fw={500}>复制范围</Text>
            {CLONE_DOMAINS.map(([value, label]) => (
              <Checkbox key={value} size="xs" label={label}
                checked={domains.includes(value)}
                onChange={(e) => setDomains(e.currentTarget.checked
                  ? [...domains, value]
                  : domains.filter((v) => v !== value))} />
            ))}
          </Stack>
        </Group>

        <Group gap="xl">
          <Checkbox size="xs" label="自动解决可安全处理的冲突" checked={autoResolve}
            onChange={(e) => setAutoResolve(e.currentTarget.checked)} />
          <Checkbox size="xs" label="下发成功后保存配置" checked={saveConfig}
            onChange={(e) => setSaveConfig(e.currentTarget.checked)} />
        </Group>

        <Group>
          <Button variant="default" leftSection={<IconCopy size={14} />} loading={previewing} onClick={doPreview}>
            生成复制方案
          </Button>
          <Button color="teal" leftSection={<IconSend size={14} />} loading={deploying}
            disabled={!preview?.preview_id || blocking} onClick={doDeploy}>
            确认复制下发
          </Button>
        </Group>

        {preview && (
          <Stack gap="sm">
            {(conflicts.errors || []).map((item, i) => (
              <Text key={i} size="sm" c="red">× {item.message || String(item)}</Text>
            ))}
            {[...warnings, ...(conflicts.warnings || []).map((w) => w.message || String(w))].map((message, i) => (
              <Text key={i} size="sm" c="yellow">! {message}</Text>
            ))}
            {Object.entries(preview.targets || {}).map(([ip, target]) => (
              <Stack key={ip} gap={4}>
                <Group justify="space-between">
                  <Text size="sm" fw={500}>{target.device_name || ip} ({ip})</Text>
                  <Text size="xs" c="dimmed">
                    {(target.commands || []).length} 条命令 · 自动处理 {(target.resolutions || []).length} 项 · 跳过 {(target.skipped || []).length} 项
                  </Text>
                </Group>
                {(target.resolutions || []).slice(0, 12).map((item, i) => (
                  <Text key={i} size="xs" c="teal">✓ {item.message || ""}</Text>
                ))}
                {(target.skipped || []).filter((item) => item.type !== "domain_not_selected").slice(0, 8).map((item, i) => (
                  <Text key={i} size="xs" c="dimmed">– {item.message || ""}</Text>
                ))}
                <MonoPre>{(target.commands || []).join("\n") || "无需下发配置"}</MonoPre>
              </Stack>
            ))}
          </Stack>
        )}

        {result && (
          <Stack gap="sm">
            <Text size="sm" fw={700} c={result.success ? "teal" : "red"}>
              {result.success ? "配置复制下发成功" : "配置复制下发未全部成功"}
              {result.summary?.total !== undefined && ` · 成功 ${result.summary.success || 0}/${result.summary.total}`}
            </Text>
            {result.error && <Text size="sm" c="red">错误: {result.error}</Text>}
            {Object.entries(result.results || {}).map(([ip, dr]) => (
              <Group key={ip} justify="space-between">
                <Text size="sm">{dr.device_name || ip} ({ip})</Text>
                <Text size="sm" c={dr.success ? "teal" : "red"} style={{ whiteSpace: "pre-wrap" }}>
                  {dr.success ? "成功" : (dr.errors || []).join("\n")}
                </Text>
              </Group>
            ))}
          </Stack>
        )}
      </Stack>
    </Paper>
  )
}
