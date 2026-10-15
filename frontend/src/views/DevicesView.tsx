import React, { useState } from "react"
import {
  Alert, Badge, Button, Checkbox, Code, Group, NumberInput, Paper, Select, Stack, Table, Text, TextInput, Title,
} from "@mantine/core"
import {
  IconAlertTriangle, IconDeviceDesktopAnalytics, IconFolder, IconLink, IconPlayerPlay, IconPlayerStop, IconPlugConnected, IconPlus, IconRefresh, IconTopologyStar3, IconTrash,
} from "@tabler/icons-react"
import { notifications } from "@mantine/notifications"
import { useApp } from "../store"
import { api } from "../api"
import type { DeviceInfo, TopologyLink } from "../types"
import { BatchStatusBanner, OnlineBadge, useConfirm } from "../ui"

// 型号分类：与后端 DEVICE_MODELS 的 kind 字段对应
const KIND_LABELS: Record<string, string> = {
  router: "路由器", l3_switch: "三层交换机", switch: "交换机",
  firewall: "防火墙", wlan_ac: "无线控制器", wlan_ap: "无线接入点",
  terminal: "终端设备",
}
const KIND_ORDER = ["router", "l3_switch", "switch", "firewall", "wlan_ac", "wlan_ap", "terminal"]

interface ModelOption { model: string; kind: string; terminal: boolean; interfaces: number }

// /api/ensp/live 的返回结构（后端 probe_ensp_live）
interface EnspLive {
  state: "not_running" | "unreadable" | "empty" | "topology_open"
  ensp_running: boolean
  injected: boolean
  pid?: number
  devices: number
  links?: number
  running?: number
  stopped?: number
  topo_path?: string | null
  live_devices?: Array<{ name: string; model: string; power_state: "running" | "stopped" | "starting" }> | null
  note?: string
  // 与项目当前激活拓扑的比对结果（后端 compare_with_active_topology）
  in_sync?: { status: "in_sync" | "not_running" | "empty" | "drifted" | "unknown"; message: string }
}

// 每种状态对应的提示文案与配色
const LIVE_HINT: Record<EnspLive["state"], { color: string; text: string }> = {
  not_running:   { color: "gray",   text: "eNSP 未运行（点击「开机」会自动拉起）" },
  unreadable:    { color: "orange", text: "eNSP 在运行但无法读取其状态（可能需要管理员权限）" },
  empty:         { color: "blue",   text: "eNSP 已就绪，但当前没有打开任何拓扑" },
  topology_open: { color: "teal",   text: "eNSP 已打开拓扑" },
}

// 后端不可用时的最小兜底，保证表单仍可用
// 是否显示「加载综合园区网案例」按钮（预设导入功能）
const SHOW_DEMO_PRESET = false

const FALLBACK_MODELS: ModelOption[] = [
  { model: "Router", kind: "router", terminal: false, interfaces: 10 },
  { model: "S5700", kind: "l3_switch", terminal: false, interfaces: 24 },
  { model: "PC", kind: "terminal", terminal: true, interfaces: 1 },
]

export function DevicesView() {
  const {
    devices, devicesRevision, collectionResults, loadDevices,
    saveDevice, deleteDevice, pingDevice, collectDevice, collectAll, showStatus,
    links, topologies, workspaceRoot, activeTopology,
    loadLinks, addLink, removeLink,
    loadTopologyLibrary, activateTopology, createTopology, deleteTopology, setWorkspaceRoot,
  } = useApp()

  const confirm = useConfirm()
  const [modelOpts, setModelOpts] = useState<ModelOption[]>(FALLBACK_MODELS)
  const [live, setLive] = useState<EnspLive | null>(null)
  const [formOpen, setFormOpen] = useState(false)


  // 从后端拉取真实支持的型号，按类别分组呈现
  React.useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const data = await api<{ models?: ModelOption[] }>("/api/ensp/status")
        if (!cancelled && data.models?.length) setModelOpts(data.models)
      } catch {
        // 后端不可用时保留兜底表
      }
    })()
    return () => { cancelled = true }
  }, [])

  const modelGroups = React.useMemo(() => {
    const byKind = new Map<string, ModelOption[]>()
    for (const m of modelOpts) {
      if (!byKind.has(m.kind)) byKind.set(m.kind, [])
      byKind.get(m.kind)!.push(m)
    }
    return KIND_ORDER
      .filter((k) => byKind.has(k))
      .map((k) => ({
        group: KIND_LABELS[k] || k,
        items: byKind.get(k)!.map((m) => ({
          value: m.model,
          label: m.terminal ? `${m.model}（终端，无 Console）` : `${m.model}`,
        })),
      }))
  }, [modelOpts])
  const [editingIp, setEditingIp] = useState<string | null>(null)
  const [form, setForm] = useState({
    name: "",
    model: "Router",
    console_port: "",
    auto_start: true,
  })
  const [saving, setSaving] = useState(false)
  // 连线编辑器
  const [linkOpen, setLinkOpen] = useState(false)
  const [linkA, setLinkA] = useState<{ device: string; interface: string }>({ device: "", interface: "" })
  const [linkB, setLinkB] = useState<{ device: string; interface: string }>({ device: "", interface: "" })
  const [ifacesA, setIfacesA] = useState<string[]>([])
  const [ifacesB, setIfacesB] = useState<string[]>([])
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [attaching, setAttaching] = useState<"match_by_devices" | "import_as" | null>(null)
  const [powering, setPowering] = useState<string | null>(null)

  const refreshLive = React.useCallback(async () => {
    try {
      setLive(await api<EnspLive>("/api/ensp/live"))
    } catch {
      setLive(null)
    }
  }, [])

  // 需求③：单台启停
  const powerDevice = React.useCallback(async (
    device: { name: string; ip: string },
    action: "start" | "stop",
  ) => {
    setPowering(`${device.ip}:${action}`)
    try {
      const res = await api<{ success: boolean; message?: string; detail?: string }>(
        `/api/ensp/devices/${encodeURIComponent(device.ip)}/${action}`,
        { method: "POST" },
      )
      notifications.show({
        color: res.success ? "teal" : "orange",
        message: res.message || res.detail || (action === "start" ? "已发送开机指令" : "已发送停机指令"),
      })
      if (res.success) refreshLive()
    } catch (e) {
      notifications.show({ color: "red", message: (e as Error).message })
    } finally {
      setPowering(null)
    }
  }, [refreshLive])

  // 需求②反向：把 eNSP 当前拓扑对齐到项目
  const attachActive = React.useCallback(async (mode: "match_by_devices" | "import_as", name?: string) => {
    setAttaching(mode)
    try {
      const res = await api<{ success: boolean; message?: string; error?: string; hint?: string }>(
        "/api/ensp/attach-active",
        { method: "POST", body: JSON.stringify({ mode, name }) },
      )
      if (res.success) {
        notifications.show({ color: "teal", message: res.message || "已对齐" })
        await Promise.all([loadDevices(), loadLinks(), loadTopologyLibrary(), refreshLive()])
      } else {
        notifications.show({ color: "orange", message: res.error + (res.hint ? `（${res.hint}）` : "") })
      }
    } catch (e) {
      notifications.show({ color: "red", message: (e as Error).message })
    } finally {
      setAttaching(null)
    }
  }, [loadDevices, loadLinks, loadTopologyLibrary, refreshLive])

  // 启动时加载连线与拓扑库
  React.useEffect(() => {
    loadLinks()
    loadTopologyLibrary()
  }, [loadLinks, loadTopologyLibrary])

  // 选中设备后拉取其可用接口
  const loadIfaces = React.useCallback(async (deviceName: string, setter: (v: string[]) => void) => {
    if (!deviceName) { setter([]); return }
    try {
      const data = await api<{ interfaces: string[] }>(`/api/topology/interfaces/${encodeURIComponent(deviceName)}`)
      setter(data.interfaces || [])
    } catch { setter([]) }
  }, [])

  const submitLink = async () => {
    if (!linkA.device || !linkA.interface || !linkB.device || !linkB.interface) {
      notifications.show({ color: "red", message: "请完整选择两端的设备与接口" }); return
    }
    try {
      await addLink(linkA, linkB)
      setLinkA({ device: "", interface: "" }); setLinkB({ device: "", interface: "" })
      setIfacesA([]); setIfacesB([])
      notifications.show({ color: "teal", message: "连线已添加" })
    } catch (e) {
      notifications.show({ color: "red", message: (e as Error).message })
    }
  }

  const removeLinkRow = async (lnk: TopologyLink) => {
    const ok = await confirm("删除连线", `${lnk.a.device}/${lnk.a.interface} ↔ ${lnk.b.device}/${lnk.b.interface}，确认删除？`)
    if (!ok) return
    try {
      await removeLink(lnk.a, lnk.b)
      notifications.show({ color: "teal", message: "连线已删除" })
    } catch (e) {
      notifications.show({ color: "red", message: (e as Error).message })
    }
  }

  // 实时监听 eNSP：让界面反映 eNSP 的真实状态，而不是本地 devices.json。
  // 动作进行中暂停轮询（避免与注入/开机抢控制端口），动作结束后立即刷新一次。
  React.useEffect(() => {
    if (busyAction) return
    let cancelled = false
    const tick = async () => {
      try {
        const data = await api<EnspLive>("/api/ensp/live")
        if (!cancelled) setLive(data)
      } catch {
        if (!cancelled) setLive(null)
      }
    }
    tick()
    const timer = setInterval(tick, 3000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [busyAction])

  const openAdd = () => {
    setEditingIp(null)
    setForm({ name: "", model: "Router", console_port: "", auto_start: true })
    setFormOpen(true)
  }

  const openEdit = (device: DeviceInfo) => {
    setEditingIp(device.ip)
    setForm({
      name: device.name || "",
      model: device.model || "Router",
      console_port: device.console_port ? String(device.console_port) : "",
      auto_start: true,
    })
    setFormOpen(true)
  }

  const submit = async () => {
    if (!form.name.trim()) {
      notifications.show({ color: "red", message: "请填写设备名称" })
      return
    }
    setSaving(true)
    try {
      const port = parseInt(form.console_port, 10)
      const payload: DeviceInfo = {
        name: form.name.trim(),
        ip: editingIp || (isNaN(port) ? "" : `127.0.0.1:${port}`),
        model: form.model || "Router",
        console_port: isNaN(port) ? null : port,
        auto_start: form.auto_start,
      }
      await saveDevice(payload, editingIp)
      setFormOpen(false)
      notifications.show({
        color: "teal",
        message: form.auto_start
          ? `设备 ${payload.name} 已添加并自动同步至 eNSP 启动`
          : `设备 ${payload.name} 已保存`,
      })
    } catch (e) {
      notifications.show({ color: "red", message: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  const remove = async (device: DeviceInfo) => {
    const ok = await confirm("删除设备", `确定删除设备 ${device.name} (${device.ip})？`)
    if (!ok) return
    try {
      await deleteDevice(device.ip)
    } catch (e) {
      notifications.show({ color: "red", message: (e as Error).message })
    }
  }

  const applyComprehensiveDemo = async (preset: "comprehensive_campus" | "big_campus" = "comprehensive_campus") => {
    const big = preset === "big_campus"
    setBusyAction("demo")
    showStatus("loading", big
      ? "正在生成 15 台设备分层园区网拓扑（核心/汇聚/接入 + 出口 + 无线 + 终端）并在 eNSP 中自动开机..."
      : "正在生成综合园区网拓扑 (R1+R2+SW1+AC1+SW2) 并在 eNSP 中自动开机...")
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const res: any = await api("/api/ensp/topo/apply", {
        method: "POST",
        body: JSON.stringify({
          preset,
          revision: devicesRevision,
          auto_start: true,
        }),
      })
      await loadDevices()
      showStatus(res.success === false ? "error" : "success",
                 res.message || (big ? "15 台设备分层园区网已加载并在 eNSP 中触发自动开机"
                   : "综合园区网拓扑已加载并在 eNSP 中触发自动开机"))
    } catch (e) {
      showStatus("error", `加载综合案例失败: ${(e as Error).message}`)
    } finally {
      setBusyAction(null)
      refreshLive()
    }
  }

  const startAllDevices = async () => {
    setBusyAction("start")
    showStatus("loading", "正在同步拓扑并向 eNSP 发送全部设备开机指令...")
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const res: any = await api("/api/ensp/start-all", { method: "POST" })
      showStatus(res.success ? "success" : "error", res.message || (res.success ? "已触发全部设备开机" : "启动失败"))
    } catch (e) {
      showStatus("error", `启动失败: ${(e as Error).message}`)
    } finally {
      setBusyAction(null)
      refreshLive()
    }
  }

  const stopAllDevices = async () => {
    setBusyAction("stop")
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const res: any = await api("/api/ensp/stop-all", { method: "POST" })
      showStatus(res.success ? "info" : "error", res.message || (res.success ? "已触发全部设备停止" : "停止失败"))
    } catch (e) {
      showStatus("error", `停止失败: ${(e as Error).message}`)
    } finally {
      setBusyAction(null)
      refreshLive()
    }
  }

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      {/* 拓扑库：切换/新建/删除拓扑，显示工作目录 */}
      <Group gap="xs" mb="sm" wrap="wrap" align="center">
        <IconFolder size={16} />
        <Text size="sm" fw={500}>拓扑：</Text>
        <Select
          w={220} size="xs" placeholder="-- 选择拓扑 --"
          data={topologies.map((tp) => ({
            value: tp.name,
            label: `${tp.name}（${tp.devices} 设备 / ${tp.links} 连线）`,
          }))}
          value={activeTopology}
          onChange={(v) => { if (v) activateTopology(v).catch((e) => notifications.show({ color: "red", message: (e as Error).message })) }}
        />
        <Button size="xs" variant="default" leftSection={<IconPlus size={14} />}
          onClick={() => {
            const name = window.prompt("新建拓扑名称（字母/数字/下划线/中文）")
            if (name) createTopology(name.trim()).catch((e) => notifications.show({ color: "red", message: (e as Error).message }))
          }}>
          新建
        </Button>
        <Button size="xs" variant="default" color="red" leftSection={<IconTrash size={14} />}
          disabled={!activeTopology}
          onClick={async () => {
            if (!activeTopology) return
            const ok = await confirm("删除拓扑", `将删除拓扑「${activeTopology}」及其全部设备配置，不可恢复。确认？`)
            if (ok) deleteTopology(activeTopology).catch((e) => notifications.show({ color: "red", message: (e as Error).message }))
          }}>
          删除
        </Button>
        <Text size="xs" c="dimmed" title={workspaceRoot}>
          工作目录：{workspaceRoot || "-"}
        </Text>
        <Button size="xs" variant="subtle"
          onClick={() => {
            const p = window.prompt("新的工作目录（绝对路径）", workspaceRoot)
            if (p) setWorkspaceRoot(p.trim()).catch((e) => notifications.show({ color: "red", message: (e as Error).message }))
          }}>
          更改目录
        </Button>
      </Group>

      {live && (
        <Alert
          color={LIVE_HINT[live.state].color}
          variant="light"
          mb="sm"
          icon={<IconPlugConnected size={16} />}
        >
          <Group gap="xs" wrap="wrap">
            <Text size="sm" fw={500}>{LIVE_HINT[live.state].text}</Text>
            {live.state === "topology_open" && (
              <Text size="sm" c="dimmed">
                {live.devices} 台设备 / {live.links ?? 0} 条连线
                {live.running ? ` · ${live.running} 台运行中` : ""}
                {live.topo_path ? ` · ${live.topo_path}` : ""}
              </Text>
            )}
          </Group>
          {live.state === "topology_open" && live.live_devices?.length ? (
            <Text size="xs" c="dimmed" mt={4}>
              eNSP 内实际设备：{live.live_devices.map((d) => d.name).join("、")}
            </Text>
          ) : null}
        </Alert>
      )}

      {live?.in_sync?.status === "drifted" && (
        <Alert
          color="orange"
          variant="light"
          mb="sm"
          icon={<IconAlertTriangle size={16} />}
          title="eNSP 里是另一套拓扑"
        >
          <Text size="sm">{live.in_sync.message}</Text>
          <Text size="xs" c="dimmed" mt={4}>
            你可以把项目切到与 eNSP 一致的已有拓扑，或把 eNSP 当前设备清单导入为新拓扑
            （eNSP 不暴露端口级连线，导入后需手动补连线）。
          </Text>
          <Group gap="xs" mt="sm">
            <Button size="compact-xs" variant="default" loading={attaching !== null}
              onClick={() => attachActive("match_by_devices")}>
              切换到匹配的拓扑
            </Button>
            <Button size="compact-xs" variant="default" loading={attaching !== null}
              onClick={() => {
                const name = window.prompt("导入为哪个拓扑名？", "ensp_imported")
                if (name) attachActive("import_as", name)
              }}>
              从 eNSP 导入为新拓扑
            </Button>
          </Group>
        </Alert>
      )}

      <Group justify="space-between" mb="md">
        <div>
          <Title order={3}>eNSP 设备管理（本地 Console 直连）</Title>
          <Text size="xs" c="dimmed">
            添加设备后自动同步生成 .topo 并在 eNSP 中开机，通过本地 Console 端口 (127.0.0.1:2000+) 直接配置与采集
          </Text>
        </div>
        <Group gap="xs">
          <Button
            leftSection={<IconPlus size={14} />}
            onClick={openAdd}
          >
            添加设备（自动开机）
          </Button>
          {/* 内置案例导入：7 台小案例 + 15 台分层园区网（验证批量/分层场景用） */}
          {SHOW_DEMO_PRESET && (
            <Button
              variant="light"
              color="indigo"
              leftSection={<IconTopologyStar3 size={14} />}
              loading={busyAction === "demo"}
              onClick={() => applyComprehensiveDemo("comprehensive_campus")}
            >
              加载综合园区网案例并开机
            </Button>
          )}
          <Button
            variant="light"
            color="indigo"
            leftSection={<IconTopologyStar3 size={14} />}
            loading={busyAction === "demo"}
            onClick={() => applyComprehensiveDemo("big_campus")}
          >
            加载 15 台分层园区网并开机
          </Button>
          <Button
            variant="light"
            color="teal"
            leftSection={<IconPlayerPlay size={14} />}
            loading={busyAction === "start"}
            onClick={startAllDevices}
          >
            全部开机
          </Button>
          <Button
            variant="light"
            color="orange"
            leftSection={<IconPlayerStop size={14} />}
            loading={busyAction === "stop"}
            onClick={stopAllDevices}
          >
            全部停止
          </Button>
          <Button
            variant="default"
            leftSection={<IconDeviceDesktopAnalytics size={14} />}
            onClick={() => collectAll()}
          >
            全量采集
          </Button>
          <Button
            leftSection={<IconRefresh size={14} />}
            variant="default"
            onClick={() => loadDevices()}
          >
            刷新
          </Button>
        </Group>
      </Group>

      <Stack gap="sm" mb="md">
        <BatchStatusBanner />
      </Stack>

      {formOpen && (
        <Paper withBorder p="md" mb="md" radius="md">
          <Text fw={600} mb="sm">{editingIp ? "编辑设备" : "添加 eNSP 设备并自动开机"}</Text>
          <Stack gap="sm">
            <Group grow>
              <TextInput
                label="设备名称"
                placeholder="例如 R1 / SW1 / FW1 / AC1"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.currentTarget.value })}
              />
              <Select
                label="设备型号"
                data={modelGroups}
                searchable
                value={form.model}
                onChange={(v) => setForm({ ...form, model: v || "Router" })}
              />
              <NumberInput
                label="Console 端口（留空自动分配）"
                placeholder="默认从 2000 起递增"
                value={form.console_port || ""}
                onChange={(v) => setForm({ ...form, console_port: v == null ? "" : String(v) })}
              />
            </Group>
            <Checkbox
              label="添加后自动生成 .topo 并在 eNSP 中启动设备"
              checked={form.auto_start}
              onChange={(e) => setForm({ ...form, auto_start: e.currentTarget.checked })}
            />
          </Stack>
          <Group mt="md">
            <Button loading={saving} onClick={submit}>{editingIp ? "保存修改" : "确认添加并开机"}</Button>
            <Button variant="default" onClick={() => setFormOpen(false)}>取消</Button>
          </Group>
        </Paper>
      )}

      <Table.ScrollContainer minWidth={700}>
        <Table verticalSpacing="xs" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>设备名称</Table.Th>
              <Table.Th>设备型号</Table.Th>
              <Table.Th>Console 通道</Table.Th>
              <Table.Th>运行状态</Table.Th>
              <Table.Th>采集状态</Table.Th>
              <Table.Th>操作</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {devices.length === 0 && (
              <Table.Tr>
                <Table.Td colSpan={6}>
                  <Text c="dimmed" size="sm" ta="center" py="lg">
                    暂无设备，请点击右上角「添加设备（自动开机）」
                  </Text>
                </Table.Td>
              </Table.Tr>
            )}
            {devices.map((device) => {
              const result = collectionResults[device.ip] || {}
              // 终端设备（PC/Laptop/MCS/STA）没有 Console 端口，探测不到；
              // 它们的"是否在跑"要问 eNSP 的电源状态。
              const isTerminal = device.device_type === "terminal"
              const liveDev = live?.live_devices?.find((d) => d.name === device.name)
              const terminalRunning = liveDev?.power_state === "running"
              return (
                <Table.Tr key={device.ip}>
                  <Table.Td>
                    <b>{device.name}</b>
                    {/* 设备上的 sysname 常是默认值（都叫 Huawei），只作辅助信息 */}
                    {result.info?.hostname && result.info.hostname !== device.name && (
                      <Text size="xs" c="dimmed">{result.info.hostname}</Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    {/* 型号以设备实测为准（display device），未采集时回退到拓扑配置 */}
                    <Badge variant="light" color="blue">
                      {result.info?.model || device.model || "未知"}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <Code>{device.console_port ? `127.0.0.1:${device.console_port}` : "—"}</Code>
                  </Table.Td>
                  <Table.Td>
                    {isTerminal ? (
                      // 终端无 Console：显示 eNSP 电源状态，而不是探不到的端口
                      <OnlineBadge ok={liveDev ? terminalRunning : undefined}
                        okText="已启动" failText="已停止" unknownText="未知" />
                    ) : (
                      <OnlineBadge ok={result.online} />
                    )}
                  </Table.Td>
                  <Table.Td>
                    {isTerminal ? (
                      <Text size="xs" c="dimmed">终端设备（无 CLI）</Text>
                    ) : (
                      <OnlineBadge ok={result.ok} okText="已就绪" failText="未连接" unknownText="待采集" />
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Group gap="xs">
                      <Button size="xs" onClick={() => openEdit(device)}>编辑</Button>
                      <Button
                        size="xs"
                        variant="default"
                        leftSection={<IconPlayerPlay size={13} />}
                        loading={powering === device.ip + ":start"}
                        title="在 eNSP 中启动这一台设备"
                        onClick={() => powerDevice(device, "start")}
                      >
                        开机
                      </Button>
                      <Button
                        size="xs"
                        variant="default"
                        leftSection={<IconPlayerStop size={13} />}
                        loading={powering === device.ip + ":stop"}
                        title="在 eNSP 中停止这一台设备"
                        onClick={() => powerDevice(device, "stop")}
                      >
                        停机
                      </Button>
                      {!isTerminal && (
                        <>
                          <Button
                            size="xs"
                            variant="default"
                            onClick={async () => {
                              try {
                                const r = await pingDevice(device.ip)
                                notifications.show({
                                  color: r.online ? "teal" : "red",
                                  message: r.online ? `${device.name} Console 端口在线` : `${device.name} Console 端口未就绪`,
                                })
                              } catch (e) {
                                notifications.show({ color: "red", message: (e as Error).message })
                              }
                            }}
                          >
                            检测端口
                          </Button>
                          <Button
                            size="xs"
                            variant="default"
                            leftSection={<IconDeviceDesktopAnalytics size={13} />}
                            onClick={() => collectDevice(device.ip)}
                          >
                            采集
                          </Button>
                        </>
                      )}
                      <Button size="xs" variant="light" color="red" onClick={() => remove(device)}>删除</Button>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              )
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>

      {/* 拓扑连线编辑器 */}
      <Paper withBorder p="md" mt="md" radius="md">
        <Group justify="space-between" mb="xs">
          <Group gap="xs">
            <IconLink size={16} />
            <Text fw={600} size="sm">拓扑连线</Text>
            <Badge variant="light" size="sm">{links.length} 条</Badge>
          </Group>
          <Button size="xs" variant="default" onClick={() => setLinkOpen(!linkOpen)}>
            {linkOpen ? "收起" : "编辑连线"}
          </Button>
        </Group>

        {links.length === 0 ? (
          <Text size="xs" c="dimmed">暂无连线。点击「编辑连线」添加设备之间的连接。</Text>
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>源设备</Table.Th><Table.Th>源接口</Table.Th>
                <Table.Th>目标设备</Table.Th><Table.Th>目标接口</Table.Th>
                <Table.Th>操作</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {links.map((lnk, i) => (
                <Table.Tr key={i}>
                  <Table.Td>{lnk.a.device}</Table.Td>
                  <Table.Td><Code>{lnk.a.interface}</Code></Table.Td>
                  <Table.Td>{lnk.b.device}</Table.Td>
                  <Table.Td><Code>{lnk.b.interface}</Code></Table.Td>
                  <Table.Td>
                    <Button size="compact-xs" variant="subtle" color="red"
                      onClick={() => removeLinkRow(lnk)}>删除</Button>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}

        {linkOpen && (
          <Stack gap="xs" mt="sm">
            <Group grow align="flex-end">
              <Select label="源设备" size="xs" searchable placeholder="-- 设备 --"
                data={devices.filter((d) => d.device_type !== "terminal").map((d) => ({ value: d.name, label: d.name }))}
                value={linkA.device}
                onChange={(v) => { setLinkA({ device: v || "", interface: "" }); loadIfaces(v || "", setIfacesA) }} />
              <Select label="源接口" size="xs" searchable placeholder="-- 接口 --"
                data={ifacesA} value={linkA.interface}
                onChange={(v) => setLinkA({ ...linkA, interface: v || "" })} />
              <Select label="目标设备" size="xs" searchable placeholder="-- 设备 --"
                data={devices.filter((d) => d.device_type !== "terminal").map((d) => ({ value: d.name, label: d.name }))}
                value={linkB.device}
                onChange={(v) => { setLinkB({ device: v || "", interface: "" }); loadIfaces(v || "", setIfacesB) }} />
              <Select label="目标接口" size="xs" searchable placeholder="-- 接口 --"
                data={ifacesB} value={linkB.interface}
                onChange={(v) => setLinkB({ ...linkB, interface: v || "" })} />
              <Button size="xs" onClick={submitLink}>添加连线</Button>
            </Group>
            <Text size="xs" c="dimmed">
              提示：串口只能连串口。添加后点「全部开机」即可把新拓扑下发到 eNSP。
            </Text>
          </Stack>
        )}
      </Paper>
    </Paper>
  )
}
