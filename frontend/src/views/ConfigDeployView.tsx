import React, { useEffect, useRef, useState } from "react"
import {
  Alert, Badge, Button, Checkbox, Group, Modal, Paper, Select, Stack, Text, Textarea, TextInput, Title,
} from "@mantine/core"
import { notifications } from "@mantine/notifications"
import { IconAlertTriangle, IconEye, IconFolder, IconSend } from "@tabler/icons-react"
import { ROLE_NAMES, TEMPLATE_CATEGORY_NAMES, TEMPLATE_CATEGORY_ORDER } from "../constants"
import { useApp } from "../store"
import { api } from "../api"
import { MonoPre, decodeEntities } from "../ui"
import type { BackupFile, ConfigTemplate, DeployOutcome } from "../types"
import type { TemplateParams } from "../components/FieldInput"
import type { SetTemplateParams } from "../components/FieldInput"
import { useConfirm } from "../ui"
import { collectParams, TemplateFields } from "../components/FieldInput"
import { DeployResult } from "../components/DeployResult"
import { ConsolePanel } from "./config/ConsolePanel"
import { CatalogPanel } from "./config/CatalogPanel"
import { ClonePanel } from "./config/ClonePanel"

export function ConfigDeployView() {
  const { devices, capabilityResults, roleResults, showStatus, deployBus, pendingFill, setPendingFill } = useApp()

  const [selectedIp, setSelectedIp] = useState("")
  const [templates, setTemplates] = useState<Record<string, ConfigTemplate>>({})
  const [templatesLoading, setTemplatesLoading] = useState(false)

  // 模板弹窗状态
  const [modalKey, setModalKey] = useState<string | null>(null)
  const [modalParams, setModalParams] = useState<TemplateParams>({})
  const [modalPresets, setModalPresets] = useState<{ filled: number; hints: string[] }>({ filled: 0, hints: [] })
  const [currentConfig, setCurrentConfig] = useState<{ text: string; source: string; warnings?: string[] } | null>(null) // {text, source, warnings}
  const [preview, setPreview] = useState<string | null>(null)
  const [result, setResult] = useState<DeployOutcome | null>(null)
  const [saveConfig, setSaveConfig] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [deploying, setDeploying] = useState(false)

  // 高级命令模式
  const [rawCommands, setRawCommands] = useState("")
  const [rawSave, setRawSave] = useState(false)
  const [rawPreview, setRawPreview] = useState<string | null>(null)
  const [rawResult, setRawResult] = useState<DeployOutcome | null>(null)
  const [rawDeploying, setRawDeploying] = useState(false)

  // 备份列表
  const [backups, setBackups] = useState<BackupFile[] | null>(null)
  const [backupLoading, setBackupLoading] = useState(false)

  const confirm = useConfirm()
  const selectedCap = capabilityResults[selectedIp]
  const deviceRole = selectedCap?.device_role || "unknown"
  const modalTpl = modalKey ? templates[modalKey] : null

  // 选择设备 → 按角色加载模板（对应旧版 onConfigDeviceSelected）
  const loadTemplatesFor = async (ip: string) => {
    if (!ip) { setTemplates({}); return }
    const role = capabilityResults[ip]?.device_role || "unknown"
    try {
      const data = await api(`/api/devices/capability-templates?device_role=${encodeURIComponent(role)}`)
      setTemplates(data.templates || {})
    } catch (e) {
      console.error("加载配置模板失败:", (e as Error).message)
      setTemplates({})
    }
  }

  const onDeviceSelected = async (ip: string) => {
    setSelectedIp(ip)
    setModalKey(null)
    setTemplates({})
    setBackups(null)
    setTemplatesLoading(true)
    await loadTemplatesFor(ip)
    setTemplatesLoading(false)
  }

  // 打开模板弹窗（presets 用于填空预填）
  const openTemplateModal = (key: string, presets: TemplateParams = {}, hints: string[] = []) => {
    const tpl = templates[key]
    if (!tpl) return
    const fields = tpl.fields || []
    const allowed: TemplateParams = {}
    let filled = 0
    for (const f of fields) {
      if (presets[f.name] !== undefined && presets[f.name] !== "") {
        allowed[f.name] = f.type === "checkbox" ? String(presets[f.name]).toLowerCase() === "true" : presets[f.name]
        filled++
      }
    }
    setModalKey(key)
    setModalParams(allowed)
    setModalPresets({ filled, hints })
    setCurrentConfig(null)
    setPreview(null)
    setResult(null)
    loadCurrentConfig()
  }

  const loadCurrentConfig = async () => {
    if (!selectedIp) return
    setCurrentConfig({ text: "正在获取设备配置...", source: "loading" })
    try {
      const data = await api(`/api/devices/${encodeURIComponent(selectedIp)}/current-config`)
      if (data.success && data.config) {
        const display = data.config.length > 2000 ? data.config.substring(0, 2000) + "\n\n... (配置过长，已截断)" : data.config
        setCurrentConfig({ text: display, source: data.source, warnings: data.warnings || [] })
      } else {
        setCurrentConfig({ text: "无法获取设备配置", source: "error" })
      }
    } catch (e) {
      setCurrentConfig({ text: "获取配置失败: " + (e as Error).message, source: "error" })
    }
  }

  const previewModal = async () => {
    if (!selectedIp || !modalKey || !modalTpl) return
    setPreviewing(true)
    try {
      const data = await api(`/api/devices/${encodeURIComponent(selectedIp)}/config/preview`, {
        method: "POST",
        body: JSON.stringify({ config_type: modalKey, params: collectParams(modalTpl, modalParams) }),
      })
      setPreview((data.preview_commands || []).join("\n"))
    } catch (e) {
      notifications.show({ color: "red", message: "预览失败: " + (e as Error).message })
    } finally {
      setPreviewing(false)
    }
  }

  const deployModal = async () => {
    if (!selectedIp || !modalKey || !modalTpl) return
    setDeploying(true)
    try {
      const data = await api(`/api/devices/${encodeURIComponent(selectedIp)}/config/deploy`, {
        method: "POST",
        body: JSON.stringify({
          config_type: modalKey,
          params: collectParams(modalTpl, modalParams),
          save_config: saveConfig,
        }),
      })
      setResult(data)
    } catch (e) {
      setResult({ success: false, error: (e as Error).message })
    } finally {
      setDeploying(false)
    }
  }

  // 高级命令模式
  const rawList = () => rawCommands.trim() ? rawCommands.split("\n").filter((c) => c.trim()) : []

  const previewRaw = async () => {
    if (!selectedIp) { notifications.show({ color: "red", message: "请先选择设备" }); return }
    const commands = rawList()
    if (!commands.length) { notifications.show({ color: "red", message: "请输入命令" }); return }
    try {
      const data = await api(`/api/devices/${encodeURIComponent(selectedIp)}/config/raw-preview`, {
        method: "POST",
        body: JSON.stringify({ commands }),
      })
      setRawPreview((data.preview_commands || data.commands || []).join("\n") + "\n\n--- " + (data.warning || "") + " ---")
    } catch (e) {
      notifications.show({ color: "red", message: "预览失败: " + (e as Error).message })
    }
  }

  const deployRaw = async () => {
    if (!selectedIp) { notifications.show({ color: "red", message: "请先选择设备" }); return }
    const commands = rawList()
    if (!commands.length) { notifications.show({ color: "red", message: "请输入命令" }); return }
    const ok1 = await confirm("确认下发原始命令",
      "高级命令模式将直接下发原始命令到设备，可能导致配置异常。系统会先自动备份当前配置。是否继续？")
    if (!ok1) return
    const ok2 = await confirm("二次确认", `这是最后确认。确认下发 ${commands.length} 条命令到设备？`)
    if (!ok2) return
    setRawDeploying(true)
    try {
      const data = await api(`/api/devices/${encodeURIComponent(selectedIp)}/config/raw-deploy`, {
        method: "POST",
        body: JSON.stringify({ commands, save_config: rawSave, confirm: true }),
      })
      setRawResult(data)
    } catch (e) {
      setRawResult({ success: false, error: (e as Error).message })
    } finally {
      setRawDeploying(false)
    }
  }

  // 备份列表
  const loadBackups = async () => {
    const ip = selectedIp
    if (!ip) { notifications.show({ color: "red", message: "请先选择设备" }); return }
    setBackupLoading(true)
    try {
      const data = await api(`/api/devices/${encodeURIComponent(ip)}/backups`)
      setBackups(data.backups || [])
    } catch (e) {
      console.error("加载备份列表失败:", (e as Error).message)
    } finally {
      setBackupLoading(false)
    }
  }

  // 填空配置入口（协议角色页 / AI 模板跳转调用）：注册到总线，避免闭包过期
  const fillInRef = useRef<((ip: string, configKey: string, protocolKey: string) => Promise<void>) | null>(null)
  fillInRef.current = async (ip: string, configKey: string, protocolKey: string) => {
    if (!devices.some((d) => d.ip === ip)) {
      showStatus("error", `设备 ${ip} 不在设备列表中，请先添加`)
      return
    }
    if (ip !== selectedIp) {
      await onDeviceSelected(ip)
    }
    const tpl = templates[configKey] // 注意：templates 可能刚刷新，需再读一次
    if (!tpl) {
      // onDeviceSelected 之后重新加载的模板在 state 里，这里重新请求
      try {
        const role = capabilityResults[ip]?.device_role || "unknown"
        const data = await api(`/api/devices/capability-templates?device_role=${encodeURIComponent(role)}`)
        setTemplates(data.templates || {})
        if (!(data.templates || {})[configKey]) {
          showStatus("error", `该设备角色不支持模板「${configKey}」`)
          return
        }
        applyFillIn(ip, configKey, protocolKey, data.templates[configKey])
        return
      } catch (e) {
        showStatus("error", "加载模板失败: " + (e as Error).message)
        return
      }
    }
    applyFillIn(ip, configKey, protocolKey, tpl)
  }

  const applyFillIn = (ip: string, configKey: string, protocolKey: string, tpl: ConfigTemplate) => {
    const proto = (roleResults[ip] || {}).protocols?.[protocolKey] || {} as any
    const presets = proto.presets || {}
    const hints = proto.preset_hints || []
    setModalKey(configKey)
    setModalPresets({ filled: 0, hints })
    setPreview(null)
    setResult(null)
    setCurrentConfig(null)
    loadCurrentConfig()
    // presets 按 tpl.fields 名单写入
    const fields = tpl.fields || []
    const allowed: TemplateParams = {}
    let filled = 0
    for (const f of fields) {
      if (presets[f.name] !== undefined && presets[f.name] !== "") {
        allowed[f.name] = f.type === "checkbox" ? String(presets[f.name]).toLowerCase() === "true" : presets[f.name]
        filled++
      }
    }
    setModalParams(allowed)
    setModalPresets({ filled, hints })
  }

  useEffect(() => {
    deployBus.current.fillInConfig = (ip: string, configKey: string, protocolKey: string) => fillInRef.current?.(ip, configKey, protocolKey)
    return () => { delete deployBus.current.fillInConfig }
  }, [deployBus])

  // 懒加载下由协议角色页 / AI 助教跳转而来：等本视图挂载后消费待处理请求
  useEffect(() => {
    if (pendingFill) {
      fillInRef.current?.(pendingFill.ip, pendingFill.configKey, pendingFill.protocolKey)
      setPendingFill(null)
    }
  }, [pendingFill, setPendingFill])

  const hasCapabilityResults = Object.keys(capabilityResults).length > 0

  // 模板网格按类别分组
  const categories: Record<string, [string, ConfigTemplate][]> = {}
  for (const [key, tpl] of Object.entries(templates)) {
    const cat = tpl.category || "other"
    ;(categories[cat] = categories[cat] || []).push([key, tpl])
  }

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Title order={3} mb="md">设备配置下发</Title>

      {!hasCapabilityResults && devices.length > 0 && (
        <Alert variant="light" color="yellow" mb="md">
          请先执行"设备能力识别"以获取设备角色，系统将根据设备角色显示适用的配置模板。
        </Alert>
      )}

      <Group align="center" mb="md" gap="xs">
        <Text size="sm">选择设备：</Text>
        <Select
          w={320} size="xs" searchable
          placeholder="-- 请先选择设备 --"
          data={devices.map((d) => {
            const role = capabilityResults[d.ip] ? ((ROLE_NAMES as Record<string, string>)[capabilityResults[d.ip].device_role || ""] || "") : ""
            return { value: d.ip, label: role ? `${d.name} (${d.ip}) - ${role}` : `${d.name} (${d.ip})` }
          })}
          value={selectedIp}
          onChange={(v) => onDeviceSelected(v || "")}
        />
        <Button size="xs" variant="default" leftSection={<IconFolder size={13} />} loading={backupLoading} onClick={loadBackups}>
          查看备份
        </Button>
      </Group>

      {backups && (
        <Stack gap={4} mb="md" p="xs" style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8 }}>
          <Text size="sm" fw={500}>备份文件列表</Text>
          {backups.length === 0 && <Text size="sm" c="dimmed">暂无备份文件</Text>}
          {backups.map((b) => (
            <Group key={b.filename} justify="space-between" gap="sm">
              <Text size="xs">{b.filename}</Text>
              <Text size="xs" c="dimmed">{b.size} bytes · {b.created}</Text>
            </Group>
          ))}
        </Stack>
      )}

      <Stack gap="md">
        <ConsolePanel />
        <CatalogPanel />
        <ClonePanel selectedIp={selectedIp} />

        {selectedIp && (
          <div>
            {templatesLoading && <Text size="sm" c="dimmed">加载配置模板中…</Text>}
            {!templatesLoading && Object.keys(templates).length > 0 && (
              <Stack gap="sm">
                {TEMPLATE_CATEGORY_ORDER.filter((cat) => categories[cat]?.length).map((cat: string) => (
                  <div key={cat}>
                    <Text size="sm" fw={600} mb={6}>{(TEMPLATE_CATEGORY_NAMES as Record<string, string>)[cat] || "其他"}</Text>
                    <Group gap="xs" align="stretch">
                      {categories[cat].map(([key, tpl]) => {
                        const cap = selectedCap?.capabilities?.[key]
                        const disabled = cap ? cap.supported === false : false
                        return (
                          <Paper key={key} withBorder p="sm"
                            onClick={() => !disabled && openTemplateModal(key)}
                            style={{
                              cursor: disabled ? "not-allowed" : "pointer",
                              minWidth: 150,
                              textAlign: "center",
                              opacity: disabled ? 0.5 : 1,
                            }}
                            title={disabled ? "设备不支持此功能" : undefined}>
                            <Text size="lg">{decodeEntities(tpl.icon) || ""}</Text>
                            <Text size="sm">{tpl.name}</Text>
                            {disabled && <Badge color="red" variant="light" size="sm" mt={4}>不支持</Badge>}
                          </Paper>
                        )
                      })}
                    </Group>
                  </div>
                ))}
              </Stack>
            )}
            {!templatesLoading && selectedIp && Object.keys(templates).length === 0 && (
              <Text size="sm" c="dimmed">该设备角色暂无可用配置模板</Text>
            )}
          </div>
        )}

        {/* 高级命令模式 */}
        <Paper withBorder p="md" radius="md">
          <Group gap={8} mb="sm">
            <IconAlertTriangle size={16} color="var(--mantine-color-red-6)" />
            <Text fw={600} size="sm">高级命令模式（危险）</Text>
          </Group>
          <Alert variant="light" color="red" mb="sm">
            警告：高级命令模式将直接下发原始命令到设备，可能导致配置异常。下发前系统将自动备份当前配置。
          </Alert>
          <Textarea
            minRows={6} styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }}
            placeholder={"每行一条命令，例如：\nsystem-view\ninterface GigabitEthernet0/0/0\nip address 10.0.0.1 255.255.255.0\nreturn"}
            value={rawCommands}
            onChange={(e) => setRawCommands(e.currentTarget.value)}
          />
          <Group mt="sm" gap="md">
            <Checkbox label="下发后保存配置" checked={rawSave} onChange={(e) => setRawSave(e.currentTarget.checked)} />
            <Button variant="default" leftSection={<IconEye size={14} />} onClick={previewRaw}>预览</Button>
            <Button color="red" leftSection={<IconSend size={14} />} loading={rawDeploying} onClick={deployRaw}>
              确认下发（危险）
            </Button>
          </Group>
          {rawPreview && <MonoPre>{rawPreview}</MonoPre>}
          {rawResult && <DeployResult data={rawResult} ip={selectedIp} />}
        </Paper>
      </Stack>

      {/* 模板配置弹窗 */}
      <Modal
        opened={!!modalKey}
        onClose={() => setModalKey(null)}
        title={
          <Text fw={600}>
            {modalTpl?.name || "配置"}
            {modalPresets.filled > 0 && <Text component="span" size="sm" c="dimmed">（已预填 {modalPresets.filled} 项）</Text>}
          </Text>
        }
        size="lg" scrollAreaComponent={undefined}
      >
        <Stack gap="sm">
          {modalPresets.hints.length > 0 && (
            <Alert variant="light" color="blue">
              预填说明: {modalPresets.hints.join("；")}。请补齐其余空格后生成预览。
            </Alert>
          )}

          {currentConfig && (
            <div style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8, padding: 10 }}>
              <Group justify="space-between" mb={6}>
                <Text size="sm" fw={500}>当前设备配置</Text>
                {currentConfig.source === "backup" && <Badge color="yellow" variant="light" size="sm">历史备份</Badge>}
                {currentConfig.source === "live" && <Badge color="teal" variant="light" size="sm">实时已加载</Badge>}
                {currentConfig.source === "cache" && <Badge color="teal" variant="light" size="sm">缓存已加载</Badge>}
                {currentConfig.source === "loading" && <Badge color="gray" variant="light" size="sm">加载中...</Badge>}
                {currentConfig.source === "error" && <Badge color="red" variant="light" size="sm">获取失败</Badge>}
              </Group>
              <MonoPre style={{ maxHeight: 220 }}>{currentConfig.text}</MonoPre>
            </div>
          )}

          {modalTpl && (
            <TemplateFields
              tpl={modalTpl}
              params={modalParams}
              setParams={setModalParams}
              interfaces={selectedCap?.interfaces || []}
            />
          )}

          {preview !== null && (
            <div>
              <Text size="sm" fw={500} mb={4}>配置命令预览</Text>
              <MonoPre>{preview}</MonoPre>
            </div>
          )}

          {result && <DeployResult data={result} ip={selectedIp} />}

          <Group justify="flex-end" mt="sm">
            <Checkbox label="下发后保存配置" checked={saveConfig} onChange={(e) => setSaveConfig(e.currentTarget.checked)} />
            <Button variant="default" leftSection={<IconEye size={14} />} loading={previewing} onClick={previewModal}>
              生成预览
            </Button>
            <Button leftSection={<IconSend size={14} />} loading={deploying} onClick={deployModal}>
              确认下发
            </Button>
            <Button variant="default" onClick={() => setModalKey(null)}>关闭</Button>
          </Group>
        </Stack>
      </Modal>
    </Paper>
  )
}
