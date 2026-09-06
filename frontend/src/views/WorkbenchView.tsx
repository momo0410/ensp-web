import React, { useEffect, useRef, useState } from "react"
import {
  Button, Checkbox, Group, Paper, Stack, Text, TextInput, Textarea, Title, Badge,
} from "@mantine/core"
import { notifications } from "@mantine/notifications"
import {
  IconDeviceFloppy, IconEye, IconFolder, IconList, IconSend, IconX,
} from "@tabler/icons-react"
import { NETWORK_VAR_NAMES, WILDCARD_VAR_NAMES } from "../constants"
import { useApp } from "../store"
import { extractVariables, rowsToJson, rebuildRows, parseVarJson } from "../lib/workbench"
import type { VarRow } from "../lib/workbench"
import type { WorkbenchDeployResult, WorkbenchDeviceResult, WorkbenchPreviewResult } from "../types"
import { api } from "../api"
import { BatchStatusBanner, EmptyHint, MonoPre, useConfirm } from "../ui"
import { networkToWildcardJS } from "../netutil"

export function WorkbenchView() {
  const { devices, workbenchBus, setLastDeploy, pendingAppend, setPendingAppend } = useApp()
  const confirm = useConfirm()

  const [taskName, setTaskName] = useState("")
  const [selectedDevices, setSelectedDevices] = useState<string[]>([])
  const [commands, setCommands] = useState("")
  const [verifyText, setVerifyText] = useState("")
  const [stopOnError, setStopOnError] = useState(true)
  const [saveAfter, setSaveAfter] = useState(false)

  const [varMode, setVarMode] = useState("table")
  const [rows, setRows] = useState<VarRow[]>([])
  const [jsonGlobal, setJsonGlobal] = useState("{}")
  const [jsonDevice, setJsonDevice] = useState("{}")

  const [previewData, setPreviewData] = useState<WorkbenchPreviewResult | null>(null) // free-preview 响应
  const [resultData, setResultData] = useState<WorkbenchDeployResult | null>(null)   // free-deploy / 任务详情响应
  const [previewing, setPreviewing] = useState(false)
  const [deploying, setDeploying] = useState(false)

  const [scriptsPanel, setScriptsPanel] = useState(false)
  const [tasksPanel, setTasksPanel] = useState(false)
  const [scripts, setScripts] = useState<{ name: string; description?: string; updated_at?: string }[]>([])
  const [tasks, setTasks] = useState<{ task_id: string; task_name?: string; status?: string; summary?: { success?: number; total?: number }; target_devices?: string[]; start_time?: string }[]>([])

  const lastPreviewRef = useRef<WorkbenchPreviewResult | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const smartDefaultsRef = useRef<Record<string, { router_id?: string }>>({})

  // AI 助教"发送到工作台"
  useEffect(() => {
    workbenchBus.current.appendCommands = (text) => {
      setCommands((prev) => (prev ? prev.replace(/\s*$/, "\n\n") : "") + text.trim() + "\n")
    }
    return () => { delete workbenchBus.current.appendCommands }
  }, [workbenchBus])

  // 懒加载下从 AI 助教跳转而来：等本视图挂载后消费待处理文本
  useEffect(() => {
    if (pendingAppend) {
      workbenchBus.current.appendCommands?.(pendingAppend)
      setPendingAppend(null)
    }
  }, [pendingAppend, setPendingAppend, workbenchBus])

  // 命令模板变化（600ms 防抖）：提取变量重建行 + 智能默认值
  const onCommandsInput = (value: string) => {
    setCommands(value)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(async () => {
      if (varMode !== "table") return
      setRows((prev) => rebuildRows(value, prev))
      await loadSmartDefaults()
    }, 600)
  }

  const loadSmartDefaults = async () => {
    if (!selectedDevices.length) return
    try {
      const data = await api("/api/config/smart-defaults", {
        method: "POST",
        body: JSON.stringify({ target_devices: selectedDevices }),
      })
      if (data.success && data.defaults) {
        smartDefaultsRef.current = data.defaults
        setRows((prev) => prev.map((row) => {
          const name = row.name.trim()
          if (name !== "router_id" && name !== "router-id") return row
          const nextDevices = { ...row.devices }
          for (const [ip, d] of Object.entries(data.defaults as Record<string, { router_id?: string }>)) {
            if (d.router_id && !(nextDevices[ip] || "").trim()) nextDevices[ip] = d.router_id
          }
          let global = row.global
          if (!global.trim()) {
            const firstIp = Object.keys(data.defaults)[0]
            if (firstIp && data.defaults[firstIp].router_id) global = data.defaults[firstIp].router_id
          }
          return { ...row, global, devices: nextDevices }
        }))
      }
    } catch (e) {
      console.warn("加载智能默认值失败:", (e as Error).message)
    }
  }

  // 设备格输入：network 变量自动推导反掩码到 wildcard 行的全局值 + 同步值
  const onCellInput = (rowIndex: number, ip: string, value: string) => {
    setRows((prev) => {
      const next = prev.map((r) => {
        if (r.devices[ip] === undefined) r.devices = { ...r.devices }
        return r
      })
      next[rowIndex] = { ...next[rowIndex], devices: { ...next[rowIndex].devices, [ip]: value } }

      const varName = next[rowIndex].name.trim()
      if (NETWORK_VAR_NAMES.includes(varName) && networkToWildcardJS(value)) {
        const networkGlobal = next[rowIndex].global.trim()
        return next.map((r) => {
          if (WILDCARD_VAR_NAMES.includes(r.name.trim()) && !r.global.trim() && networkGlobal) {
            const wc = networkToWildcardJS(networkGlobal)
            if (wc) return { ...r, global: wc }
          }
          return r
        })
      }
      return next
    })
  }

  const renameRow = (rowIndex: number, newName: string) => {
    setRows((prev) => prev.map((r, i) => (i === rowIndex ? { ...r, name: newName } : r)))
  }

  const deleteRow = (rowIndex: number) => setRows((prev) => prev.filter((_, i) => i !== rowIndex))

  const addRow = () => setRows((prev) => [...prev, { name: "", global: "", devices: {} }])

  const switchMode = () => {
    if (varMode === "table") {
      const { globalVars, deviceVars } = rowsToJson(rows)
      setJsonGlobal(JSON.stringify(globalVars, null, 2))
      setJsonDevice(JSON.stringify(deviceVars, null, 2))
      setVarMode("json")
    } else {
      let globalVars = {}
      const parsed = parseVarJson(jsonGlobal, jsonDevice, commands)
      if (parsed) setRows(parsed)
      setVarMode("table")
    }
  }

  const buildData = () => {
    const errors = []
    let globalVars = {}
    let deviceVars = {}
    if (varMode === "table") {
      const r = rowsToJson(rows)
      globalVars = r.globalVars
      deviceVars = r.deviceVars
    } else {
      try { if (jsonGlobal.trim() && jsonGlobal.trim() !== "{}") globalVars = JSON.parse(jsonGlobal) } catch (e) {
        errors.push(`全局变量 JSON 解析失败: ${(e as Error).message}`)
      }
      try { if (jsonDevice.trim() && jsonDevice.trim() !== "{}") deviceVars = JSON.parse(jsonDevice) } catch (e) {
        errors.push(`单设备变量 JSON 解析失败: ${(e as Error).message}`)
      }
    }
    return {
      task_name: taskName.trim(),
      target_devices: selectedDevices,
      command_template: commands,
      global_vars: globalVars,
      device_vars: deviceVars,
      verify_commands: verifyText.trim() ? verifyText.split("\n").map((s) => s.trim()).filter(Boolean) : [],
      save_after_success: saveAfter,
      stop_on_error: stopOnError,
      errors,
    }
  }

  const doPreview = async () => {
    const data = buildData()
    if (data.errors.length) { notifications.show({ color: "red", message: data.errors.join("\n") }); return }
    if (!data.target_devices.length) { notifications.show({ color: "red", message: "请选择至少一台目标设备" }); return }
    if (!data.command_template.trim()) { notifications.show({ color: "red", message: "命令模板不能为空" }); return }
    setPreviewing(true)
    try {
      const result = await api("/api/config/free-preview", {
        method: "POST",
        body: JSON.stringify({
          task_name: data.task_name,
          target_devices: data.target_devices,
          command_template: data.command_template,
          global_vars: data.global_vars,
          device_vars: data.device_vars,
          verify_commands: data.verify_commands,
        }),
      })
      if (!result.success) {
        notifications.show({ color: "red", message: "预览失败:\n" + (result.errors || []).join("\n") })
        return
      }
      lastPreviewRef.current = result
      setPreviewData(result)
      setResultData(null)
    } catch (e) {
      notifications.show({ color: "red", message: "预览请求失败: " + (e as Error).message })
    } finally {
      setPreviewing(false)
    }
  }

  const doDeploy = async () => {
    if (!lastPreviewRef.current) { notifications.show({ color: "red", message: "请先生成预览" }); return }
    const data = buildData()
    if (data.errors.length) { notifications.show({ color: "red", message: data.errors.join("\n") }); return }
    const saveText = data.save_after_success ? "并保存配置" : ""
    const ok = await confirm("确认下发配置",
      `即将向 ${data.target_devices.length} 台设备下发配置${saveText}。\n系统会先自动备份每台设备的当前配置。\n\n是否确认继续？`)
    if (!ok) return
    setDeploying(true)
    try {
      const result = await api("/api/config/free-deploy", {
        method: "POST",
        body: JSON.stringify({
          task_name: data.task_name,
          target_devices: data.target_devices,
          command_template: data.command_template,
          global_vars: data.global_vars,
          device_vars: data.device_vars,
          verify_commands: data.verify_commands,
          save_after_success: data.save_after_success,
          stop_on_error: data.stop_on_error,
        }),
      })
      setResultData(result)
      setLastDeploy({
        ip: "",
        success: !!result.success,
        error: result.success ? "" : `自由配置任务执行失败（成功 ${result.summary?.success || 0}/${result.summary?.total || 0}）`,
        outputs: Object.entries((result.results || {}) as Record<string, WorkbenchDeviceResult>)
          .map(([ip, r]) => `${r.success ? "✓" : "✗"} ${r.device_name || ip}\n${(r.errors || []).join("\n") || (r.command_output || "").slice(0, 500)}`)
          .join("\n\n").slice(0, 2000),
      })
    } catch (e) {
      notifications.show({ color: "red", message: "下发请求失败: " + (e as Error).message })
    } finally {
      setDeploying(false)
    }
  }

  const saveScript = async () => {
    const data = buildData()
    let name = data.task_name
    if (!name) {
      name = window.prompt("请输入脚本名称:") || ""
      if (!name) return
    }
    if (!data.command_template.trim()) { notifications.show({ color: "red", message: "命令模板不能为空" }); return }
    try {
      const result = await api("/api/scripts", {
        method: "POST",
        body: JSON.stringify({
          script_name: name,
          description: "",
          command_template: data.command_template,
          global_vars: data.global_vars,
          sample_device_vars: data.device_vars,
          verify_commands: data.verify_commands,
        }),
      })
      if (result.success) notifications.show({ color: "teal", message: `脚本 '${name}' 已保存` })
      else notifications.show({ color: "red", message: "保存失败: " + (result.message || "未知错误") })
    } catch (e) {
      notifications.show({ color: "red", message: "保存请求失败: " + (e as Error).message })
    }
  }

  const openScripts = async () => {
    try {
      const data = await api("/api/scripts")
      setScripts(data.scripts || [])
      setScriptsPanel(true)
      setTasksPanel(false)
    } catch (e) {
      notifications.show({ color: "red", message: "加载脚本列表失败: " + (e as Error).message })
    }
  }

  const loadScript = async (name: string) => {
    try {
      const data = await api(`/api/scripts/${encodeURIComponent(name)}`)
      if (!data.success) { notifications.show({ color: "red", message: "加载失败: " + (data.message || "未知错误") }); return }
      const s = data.script
      setTaskName(s.script_name || "")
      setCommands(Array.isArray(s.command_template) ? s.command_template.join("\n") : (s.command_template || ""))
      setJsonGlobal(JSON.stringify(s.global_vars || {}, null, 2))
      setJsonDevice(JSON.stringify(s.sample_device_vars || {}, null, 2))
      setVerifyText((s.verify_commands || []).join("\n"))
      setRows([])
      setVarMode("json")
      setScriptsPanel(false)
      notifications.show({ color: "teal", message: `脚本 '${s.script_name}' 已加载（JSON 变量模式）` })
    } catch (e) {
      notifications.show({ color: "red", message: "加载脚本失败: " + (e as Error).message })
    }
  }

  const deleteScript = async (name: string) => {
    const ok = await confirm("删除脚本", `确定删除脚本 '${name}' 吗？`)
    if (!ok) return
    try {
      const result = await api(`/api/scripts/${encodeURIComponent(name)}`, { method: "DELETE" })
      if (result.success) {
        notifications.show({ color: "teal", message: `脚本 '${name}' 已删除` })
        openScripts()
      } else notifications.show({ color: "red", message: "删除失败: " + (result.message || "未知错误") })
    } catch (e) {
      notifications.show({ color: "red", message: "删除请求失败: " + (e as Error).message })
    }
  }

  const openTasks = async () => {
    try {
      const data = await api("/api/tasks")
      setTasks(data.tasks || [])
      setTasksPanel(true)
      setScriptsPanel(false)
    } catch (e) {
      notifications.show({ color: "red", message: "加载历史记录失败: " + (e as Error).message })
    }
  }

  const viewTaskDetail = async (taskId: string) => {
    try {
      const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`)
      if (!data.success) { notifications.show({ color: "red", message: "加载失败: " + (data.message || "未知错误") }); return }
      const t = data.task
      setResultData({
        success: t.status === "success",
        task_id: t.task_id,
        task_name: t.task_name,
        summary: t.summary || {},
        results: t.results || {},
        warnings: [],
      })
      setTasksPanel(false)
    } catch (e) {
      notifications.show({ color: "red", message: "加载任务详情失败: " + (e as Error).message })
    }
  }

  const onDeviceToggle = (ip: string, checked: boolean) => {
    setSelectedDevices((prev) => (checked ? [...prev, ip] : prev.filter((d) => d !== ip)))
    setPreviewData(null)
    setResultData(null)
    lastPreviewRef.current = null
  }

  const conflicts = previewData?.conflicts
  const hasBlockingErrors = (conflicts?.errors?.length || 0) > 0

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3}>自由配置工作台</Title>
        <Group gap="xs">
          <Button size="xs" variant="default" leftSection={<IconFolder size={13} />} onClick={openScripts}>加载脚本</Button>
          <Button size="xs" variant="default" leftSection={<IconList size={13} />} onClick={openTasks}>历史记录</Button>
        </Group>
      </Group>

      <Stack gap="sm">
        <BatchStatusBanner />

        <TextInput label="任务名称" placeholder="例如：批量配置管理地址" w={320}
          value={taskName} onChange={(e) => setTaskName(e.currentTarget.value)} />

        <div>
          <Text size="sm" fw={500} mb={6}>目标设备（多选）</Text>
          {devices.length === 0 && <Text size="xs" c="dimmed">暂无设备，请先扫描或添加设备</Text>}
          <Group gap="xs">
            {devices.map((d) => (
              <Checkbox key={d.ip} size="xs"
                label={<span>{d.name} <Text component="span" size="xs" c="dimmed">{d.ip}</Text></span>}
                checked={selectedDevices.includes(d.ip)}
                onChange={(e) => onDeviceToggle(d.ip, e.currentTarget.checked)} />
            ))}
          </Group>
        </div>

        <div>
          <Text size="sm" fw={500} mb={4}>
            命令模板
            <Text component="span" size="xs" c="dimmed" ml={6}>支持 {"{{变量名}}"} 语法，# 开头为注释，空行自动跳过</Text>
          </Text>
          <Textarea minRows={10} styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }}
            placeholder={"interface {{interface}}\nip address {{ip_address}} {{subnet_mask}}\nquit\n\n# 开启 LLDP\nlldp enable"}
            value={commands}
            onChange={(e) => onCommandsInput(e.currentTarget.value)} />
        </div>

        <div>
          <Group gap="xs" mb={4}>
            <Text size="sm" fw={500}>变量配置</Text>
            <Button size="xs" variant="default" onClick={addRow}>+ 添加变量</Button>
            <Button size="xs" variant="default" onClick={switchMode}>
              {varMode === "table" ? "切换到 JSON 模式" : "切换到表格模式"}
            </Button>
            <Text size="xs" c="dimmed">
              {varMode === "table" ? "表格模式 — 模板变量自动提取" : "JSON 模式 — 手动编辑全局和单设备变量"}
            </Text>
          </Group>

          {varMode === "table" ? (
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", minWidth: 480 }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: "left", padding: "6px 8px", minWidth: 140 }}>变量名</th>
                    <th style={{ textAlign: "left", padding: "6px 8px" }}>全局默认值</th>
                    {selectedDevices.map((ip) => {
                      const info = devices.find((d) => d.ip === ip) || { name: ip }
                      return <th key={ip} title={ip} style={{ textAlign: "left", padding: "6px 8px", minWidth: 120 }}>{info.name}</th>
                    })}
                    <th style={{ width: 44 }}></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 && (
                    <tr>
                      <td colSpan={3 + selectedDevices.length} style={{ textAlign: "center", padding: 14 }}>
                        <Text size="xs" c="dimmed">在命令模板中使用 {"{{变量名}}"} 后自动提取；也可点「添加变量」手动添加</Text>
                      </td>
                    </tr>
                  )}
                  {rows.map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      <td style={{ padding: "3px 4px" }}>
                        <TextInput size="xs" placeholder="变量名" value={row.name}
                          onChange={(e) => renameRow(rowIndex, e.currentTarget.value)} />
                      </td>
                      <td style={{ padding: "3px 4px" }}>
                        <TextInput size="xs" placeholder="所有设备共用" value={row.global}
                          onChange={(e) => setRows((prev) => prev.map((r, i) => (i === rowIndex ? { ...r, global: e.currentTarget.value } : r)))} />
                      </td>
                      {selectedDevices.map((ip) => (
                        <td key={ip} style={{ padding: "3px 4px" }}>
                          <TextInput size="xs" placeholder={ip} value={row.devices[ip] || ""}
                            onChange={(e) => onCellInput(rowIndex, ip, e.currentTarget.value)} />
                        </td>
                      ))}
                      <td style={{ padding: "3px 4px" }}>
                        <Button size="compact-xs" variant="light" color="red"
                          onClick={() => deleteRow(rowIndex)}><IconX size={12} /></Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Group align="flex-start" gap="md" wrap="nowrap">
              <div style={{ flex: 1 }}>
                <Text size="sm" fw={500} mb={4}>全局变量 (JSON)</Text>
                <Textarea minRows={5} styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }} value={jsonGlobal}
                  onChange={(e) => setJsonGlobal(e.currentTarget.value)}
                  placeholder='{"subnet_mask": "255.255.255.0"}' />
              </div>
              <div style={{ flex: 1 }}>
                <Text size="sm" fw={500} mb={4}>单设备变量 (JSON)</Text>
                <Textarea minRows={5} styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }} value={jsonDevice}
                  onChange={(e) => setJsonDevice(e.currentTarget.value)}
                  placeholder='{"192.168.56.11": {"hostname": "AR1"}}' />
              </div>
            </Group>
          )}
        </div>

        <div>
          <Text size="sm" fw={500} mb={4}>验证命令</Text>
          <Textarea minRows={3} styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }} value={verifyText}
            onChange={(e) => setVerifyText(e.currentTarget.value)}
            placeholder={"display ip interface brief\ndisplay current-configuration | include sysname"} />
        </div>

        <Group gap="xl">
          <Checkbox size="xs" label="某台设备失败时停止该设备后续操作" checked={stopOnError}
            onChange={(e) => setStopOnError(e.currentTarget.checked)} />
          <Checkbox size="xs" label="下发成功后保存配置" checked={saveAfter}
            onChange={(e) => setSaveAfter(e.currentTarget.checked)} />
        </Group>

        <Group>
          <Button leftSection={<IconEye size={14} />} loading={previewing} onClick={doPreview}>生成预览</Button>
          <Button color="teal" leftSection={<IconSend size={14} />} loading={deploying}
            disabled={hasBlockingErrors} onClick={doDeploy}>确认下发</Button>
          <Button variant="default" leftSection={<IconDeviceFloppy size={14} />} onClick={saveScript}>保存为脚本</Button>
        </Group>

        {previewData && (
          <div>
            <Text fw={600} size="sm" mb={6}>配置预览</Text>
            <ConflictBanner conflicts={conflicts} />
            {(previewData.warnings || []).map((w, i) => (
              <Text key={i} size="sm" c="yellow">⚠ {w}</Text>
            ))}
            {Object.keys(previewData.preview || {}).length === 0 && <Text size="sm" c="dimmed">无预览数据</Text>}
            <div style={{ display: "grid", gap: 8, gridTemplateColumns: `repeat(${Math.min(Object.keys(previewData.preview || {}).length, 4) || 1}, 1fr)` }}>
              {Object.entries(previewData.preview || {}).map(([ip, info]) => (
                <Paper key={ip} withBorder p="xs" radius="md">
                  <Group justify="space-between" mb={4}>
                    <Text size="sm" fw={500}>{info.device_name || ip}</Text>
                    <Text size="xs" c="dimmed">{(info.commands || []).length} 条命令</Text>
                  </Group>
                  <MonoPre style={{ maxHeight: 300 }}>{(info.commands || []).join("\n")}</MonoPre>
                </Paper>
              ))}
            </div>
          </div>
        )}

        {resultData && <WorkbenchResult result={resultData} />}

        {scriptsPanel && (
          <div>
            <Group justify="space-between" mb={6}>
              <Text fw={600} size="sm">脚本列表</Text>
              <Button size="xs" variant="default" onClick={() => setScriptsPanel(false)}>关闭</Button>
            </Group>
            {scripts.length === 0 && <EmptyHint>暂无保存的脚本</EmptyHint>}
            <Stack gap={4}>
              {scripts.map((s) => (
                <Group key={s.name} justify="space-between" p="xs" style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8, cursor: "pointer" }}
                  onClick={() => loadScript(s.name)}>
                  <div>
                    <Text size="sm" fw={500}>{s.name}</Text>
                    <Text size="xs" c="dimmed">{s.description || ""} | {s.updated_at || ""}</Text>
                  </div>
                  <Button size="compact-xs" variant="light" color="red"
                    onClick={(e) => { e.stopPropagation(); deleteScript(s.name) }}>×</Button>
                </Group>
              ))}
            </Stack>
          </div>
        )}

        {tasksPanel && (
          <div>
            <Group justify="space-between" mb={6}>
              <Text fw={600} size="sm">历史记录</Text>
              <Button size="xs" variant="default" onClick={() => setTasksPanel(false)}>关闭</Button>
            </Group>
            {tasks.length === 0 && <EmptyHint>暂无历史任务记录</EmptyHint>}
            <Stack gap={4}>
              {tasks.map((t) => (
                <div key={t.task_id} onClick={() => viewTaskDetail(t.task_id)}
                  style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8, padding: 8, cursor: "pointer" }}>
                  <Group justify="space-between">
                    <Text size="sm" fw={500}>{t.task_name || "未命名"}</Text>
                    <Badge size="xs" variant="light" color={t.status === "success" ? "teal" : t.status === "partial" ? "yellow" : "red"}>
                      {t.status || "unknown"}
                    </Badge>
                  </Group>
                  <Text size="xs" c="dimmed">
                    {(t.target_devices || []).length} 台设备 | 成功 {t.summary?.success || 0}/{t.summary?.total || 0} | {t.start_time || ""}
                  </Text>
                </div>
              ))}
            </Stack>
          </div>
        )}
      </Stack>
    </Paper>
  )
}

function ConflictBanner({ conflicts }: { conflicts?: WorkbenchPreviewResult["conflicts"] }) {
  if (!conflicts) return null
  const errors = conflicts.errors || []
  const warnings = conflicts.warnings || []
  const suggestions = conflicts.suggestions || []
  if (!errors.length && !warnings.length && !suggestions.length) return null
  return (
    <Stack gap={4} mb={8}>
      {errors.map((e, i) => <Text key={i} size="sm" c="red">× {e.message || String(e)}</Text>)}
      {warnings.map((w, i) => <Text key={i} size="sm" c="yellow">! {w.message || String(w)}</Text>)}
      {suggestions.map((s, i) => <Text key={i} size="sm" c="blue">i {s.message || String(s)}</Text>)}
    </Stack>
  )
}

function WorkbenchResult({ result }: { result: WorkbenchDeployResult }) {
  const summary = result.summary || {}
  const statusText = result.success ? "全部成功" : ((summary.success || 0) > 0 ? "部分成功" : "执行失败")
  const statusColor = result.success ? "teal" : ((summary.success || 0) > 0 ? "yellow" : "red")

  return (
    <div>
      <Text fw={600} size="sm" mb={6}>执行结果</Text>
      <Group gap="md" mb={8}>
        <Badge variant="light" color={statusColor}>{statusText}</Badge>
        <Text size="sm">成功: {summary.success || 0} / {summary.total || 0}</Text>
        {result.task_id && <Text size="xs" c="dimmed">任务 ID: {result.task_id}</Text>}
      </Group>

      {(result.warnings || []).map((w, i) => <Text key={i} size="sm" c="yellow">⚠ {w}</Text>)}

      <Stack gap="sm">
        {Object.entries(result.results || {}).map(([ip, info]) => (
          <Paper key={ip} withBorder p="xs" radius="md">
            <Group justify="space-between">
              <Text size="sm" fw={500} c={info.success ? "teal" : "red"}>
                {info.success ? "✓" : "×"} {info.device_name || ip}
              </Text>
              <Text size="xs" c="dimmed">
                {info.duration ? `${info.duration.toFixed(1)}s` : "-"}{info.backup_file ? " | 已备份" : ""}
              </Text>
            </Group>
            {(info.errors || []).length > 0 && (
              <div>
                <Text size="xs" fw={500} mt={4}>错误</Text>
                {(info.errors || []).map((e, i) => <Text key={i} size="xs" c="red">× {e}</Text>)}
              </div>
            )}
            {info.command_output && (
              <div>
                <Text size="xs" fw={500} mt={4}>命令回显</Text>
                <MonoPre>{String(info.command_output).substring(0, 5000)}</MonoPre>
              </div>
            )}
            {Object.entries(info.verify_outputs || {}).map(([vcmd, vout]) => (
              <div key={vcmd}>
                <Text size="xs" fw={500} mt={4}>验证: {vcmd}</Text>
                <MonoPre>{String(vout).substring(0, 3000)}</MonoPre>
              </div>
            ))}
          </Paper>
        ))}
      </Stack>
    </div>
  )
}
