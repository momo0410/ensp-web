import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import { api } from "./api"
import type {
  CapabilityResult, CapabilityResult as CapResultT, CollectionResult, DeviceInfo,
  DevicePingResult, InterfaceInfo, LastDeploy, RoleResult, TopologyLink,
} from "./types"

export type StatusType = "info" | "success" | "error" | "warning" | "loading"

export interface BatchStatus {
  type: StatusType
  message: string
}

export interface SelectedInterface {
  name: string
  ip: string
  auto?: boolean
}

// 跨视图跳转的待处理请求（配合懒加载：目标视图挂载后消费）
export interface PendingFill {
  ip: string
  configKey: string
  protocolKey: string
}

// 跨视图协作总线：视图挂载时注册自己的实现，避免闭包过期
export interface DeployBus {
  fillInConfig?: (ip: string, configKey: string, protocolKey: string) => void
}
export interface AIBus {
  openDiagnose?: () => void
  templateJump?: (key: string) => void
}
export interface WorkbenchBus {
  appendCommands?: (text: string) => void
}

interface AppContext {
  section: string
  setSection: (s: string) => void
  interfaces: InterfaceInfo[]
  selectedInterface: SelectedInterface | null
  subnet: string
  discoverSubnet: string
  setDiscoverSubnet: React.Dispatch<React.SetStateAction<string>>
  selectInterface: (name: string, ip: string, netmask: string, auto?: boolean) => Promise<boolean>
  loadInterfaces: () => Promise<boolean>
  devices: DeviceInfo[]
  collectionResults: Record<string, CollectionResult>
  capabilityResults: Record<string, CapResultT>
  roleResults: Record<string, RoleResult>
  topologyLinks: TopologyLink[]
  loadDevices: () => Promise<void>
  saveDevice: (payload: DeviceInfo, editingIp: string | null) => Promise<void>
  deleteDevice: (ip: string) => Promise<void>
  pingDevice: (ip: string) => Promise<{ online?: boolean }>
  discoverDevices: (options?: { collectAfter?: boolean; subnet?: string }) => Promise<unknown>
  collectDevice: (ip: string) => Promise<CollectionResult | null>
  collectAll: (options?: { automatic?: boolean }) => Promise<unknown>
  probeAllCapabilities: (auto?: boolean) => Promise<unknown>
  probeSingleCapability: (ip: string) => Promise<void>
  analyzeRoles: (silent?: boolean) => Promise<Record<string, RoleResult>>
  loadTopology: (silent?: boolean) => Promise<void>
  loadServerData: () => Promise<void>
  loadCollectionResults: () => Promise<void>
  loadCapabilityResults: () => Promise<void>
  batchStatus: BatchStatus | null
  showStatus: (type: StatusType, message: string) => void
  flashStatus: (type: StatusType | null, message?: string) => void
  lastDeploy: LastDeploy | null
  setLastDeploy: (d: LastDeploy | null) => void
  aiConfigured: boolean | null
  setAiConfigured: (v: boolean | null) => void
  aiDrawerOpen: boolean
  setAiDrawerOpen: (v: boolean) => void
  pendingFill: PendingFill | null
  setPendingFill: (p: PendingFill | null) => void
  pendingAppend: string | null
  setPendingAppend: (t: string | null) => void
  deployBus: React.MutableRefObject<DeployBus>
  aiBus: React.MutableRefObject<AIBus>
  workbenchBus: React.MutableRefObject<WorkbenchBus>
  devicePing: (src: string, dst: string, count?: number) => Promise<DevicePingResult>
}

const Ctx = createContext<AppContext | null>(null)

// eslint-disable-next-line react-refresh/only-export-components
export function useApp(): AppContext {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error("useApp 必须在 AppProvider 内使用")
  return ctx
}

/**
 * 全局共享状态：
 * - 设备/采集/能力/角色/拓扑/网卡 数据与加载动作（保持旧版的级联链：
 *   发现 → 批量采集 → 自动能力识别 → 静默角色分析）
 * - 批量状态条 batchStatus、最近下发结果 lastDeploy（AI 诊断数据源）
 * - 跨视图协作总线 deployBus / aiBus / workbenchBus
 */
export function AppProvider({ children }: { children: React.ReactNode }) {
  const [section, setSection] = useState("section-interfaces")

  // 网卡
  const [interfaces, setInterfaces] = useState<InterfaceInfo[]>([])
  const [selectedInterface, setSelectedInterface] = useState<SelectedInterface | null>(null)
  const [subnetState, setSubnetState] = useState("")
  const [discoverSubnet, setDiscoverSubnet] = useState("")

  // 设备与结果
  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [collectionResults, setCollectionResults] = useState<Record<string, CollectionResult>>({})
  const [capabilityResults, setCapabilityResults] = useState<Record<string, CapResultT>>({})
  const [roleResults, setRoleResults] = useState<Record<string, RoleResult>>({})
  const [topologyLinks, setTopologyLinks] = useState<TopologyLink[]>([])

  // 全局 UI 状态
  const [batchStatus, setBatchStatus] = useState<BatchStatus | null>(null)
  const [lastDeploy, setLastDeploy] = useState<LastDeploy | null>(null)
  const [aiConfigured, setAiConfigured] = useState<boolean | null>(null)
  const [aiDrawerOpen, setAiDrawerOpen] = useState(false)
  const [pendingFill, setPendingFill] = useState<PendingFill | null>(null)
  const [pendingAppend, setPendingAppend] = useState<string | null>(null)

  // 跨视图总线：视图挂载时注册自己的实现
  const deployBus = useRef<DeployBus>({})
  const aiBus = useRef<AIBus>({})
  const workbenchBus = useRef<WorkbenchBus>({})

  const flashStatus = useCallback((type: StatusType | null, message?: string) => {
    setBatchStatus(type && message ? { type, message } : null)
  }, [])

  const showStatus = useCallback((type: StatusType, message: string) => {
    setBatchStatus({ type, message })
  }, [])

  // ---------- 网卡 ----------
  const selectInterface = useCallback(async (name: string, ip: string, netmask: string, auto = false): Promise<boolean> => {
    if (!ip || !netmask) return false
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/interfaces/select", {
        method: "POST",
        body: JSON.stringify({ name, ip, netmask }),
      })
      setSelectedInterface({ name, ip, auto })
      setSubnetState(data.subnet || "")
      setDiscoverSubnet((prev: string) => prev.trim() ? prev : (data.subnet || ""))
      return true
    } catch (e) {
      console.error("选择网卡失败:", (e as Error).message)
      return false
    }
  }, [])

  const loadInterfaces = useCallback(async (): Promise<boolean> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/interfaces")
      setInterfaces(data.interfaces || [])
      const recommended = data.recommended_interface || (data.interfaces || []).find((i: InterfaceInfo) => i.is_recommended)
      if (recommended && recommended.ipv4 && recommended.netmask) {
        await selectInterface(recommended.name, recommended.ipv4, recommended.netmask, true)
      }
      return true
    } catch (e) {
      console.error("读取网卡失败:", (e as Error).message)
      return false
    }
  }, [selectInterface])

  // ---------- 设备 ----------
  const loadDevices = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/devices")
      setDevices(data.devices || [])
    } catch (e) {
      console.error("加载设备列表失败:", (e as Error).message)
    }
  }, [])

  const saveDevice = useCallback(async (payload: DeviceInfo, editingIp: string | null) => {
    if (editingIp) {
      await api(`/api/devices/${encodeURIComponent(editingIp)}`, { method: "PUT", body: JSON.stringify(payload) })
    } else {
      await api("/api/devices", { method: "POST", body: JSON.stringify(payload) })
    }
    await loadDevices()
  }, [loadDevices])

  const deleteDevice = useCallback(async (ip: string) => {
    await api(`/api/devices/${encodeURIComponent(ip)}`, { method: "DELETE" })
    setCollectionResults((prev) => {
      const next = { ...prev }
      delete next[ip]
      return next
    })
    await loadDevices()
  }, [loadDevices])

  const pingDevice = useCallback(async (ip: string) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const data: any = await api("/api/ping", { method: "POST", body: JSON.stringify({ ip }) })
    setCollectionResults((prev) => ({ ...prev, [ip]: { ...(prev[ip] || {}), online: !!data.online } }))
    return data
  }, [])

  // ---------- 发现与采集（级联链） ----------
  const getConsoleDiagnosticMessage = useCallback(async (): Promise<string> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/ensp-console-diagnostics")
      const consoleDevices = data.console_devices || []
      if (!consoleDevices.length) return ""
      if (data.cloud_bridge_issue) {
        const names = (data.cloud_issue_devices || [])
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          .map((item: any) => `${item.name}(${(item.mgmt_ips || []).join("/")})`)
          .join("、")
        return names
          ? `。Console 已检测到 ${names}，SSH 配置存在，但宿主机与管理 IP 不互通，重点检查 Cloud 到 VirtualBox Host-Only 的双向映射`
          : "。Console 已检测到设备，但宿主机与管理 IP 不互通，重点检查 Cloud 到 VirtualBox Host-Only 的双向映射"
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return `。Console 可见设备: ${consoleDevices.map((item: any) => item.name).join("、")}`
    } catch (e) {
      return ""
    }
  }, [])

  const analyzeRoles = useCallback(async (silent = false): Promise<Record<string, RoleResult>> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/protocol-roles-all", { method: "POST" })
      const map: Record<string, RoleResult> = {}
      for (const r of data.results || []) map[r.ip] = r
      setRoleResults(map)
      if (!silent && data.count === 0) {
        showStatus("warning", "暂无已识别能力的设备，请先执行能力识别")
      }
      return map
    } catch (e) {
      if (!silent) console.error("协议角色分析失败:", (e as Error).message)
      return {}
    }
  }, [showStatus])

  const probeAllCapabilities = useCallback(async (auto = false): Promise<unknown> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/devices/capabilities-all", { method: "POST" })
      const map: Record<string, CapResultT> = {}
      for (const r of data.results || []) map[r.ip] = r
      setCapabilityResults(map)
      analyzeRoles(true)
      if (auto) {
        showStatus("success",
          `采集及能力识别完成: ${(data.results || []).filter((r: CapResultT) => r.ssh_status).length}/${(data.results || []).length} 台设备识别成功`)
      }
      return data
    } catch (e) {
      console.error("能力识别失败:", (e as Error).message)
      return null
    }
  }, [analyzeRoles, showStatus])

  const probeSingleCapability = useCallback(async (ip: string) => {
    try {
      const data = await api<CapResultT>(`/api/devices/${encodeURIComponent(ip)}/capabilities`)
      setCapabilityResults((prev) => ({ ...prev, [ip]: data }))
    } catch (e) {
      console.error(`能力识别 ${ip} 失败:`, (e as Error).message)
    }
  }, [])

  const loadTopology = useCallback(async (silent = false) => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/topology")
      setTopologyLinks((data.topology && data.topology.links) || [])
      if (!silent) flashStatus(null)
    } catch (e) {
      if (!silent) showStatus("error", "加载拓扑失败: " + (e as Error).message)
    }
  }, [flashStatus, showStatus])

  const collectAll = useCallback(async (options: { automatic?: boolean } = {}): Promise<unknown> => {
    const automatic = !!options.automatic
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/collect-all", { method: "POST" })
      if (data.devices) setDevices(data.devices)
      setCollectionResults((prev) => ({ ...prev, ...(data.results || {}) }))
      const results: CollectionResult[] = Object.values(data.results || {})
      const okCount = results.filter((r) => r.ssh_ok).length
      if (!automatic) {
        showStatus("success", `批量采集完成: ${okCount}/${results.length} 台 SSH 成功${(data.lldp_added || []).length ? `，LLDP 新增 ${(data.lldp_added || []).length} 台` : ""}`)
      }
      if (automatic) loadTopology(true)
      if (okCount > 0) await probeAllCapabilities(true)
      return data
    } catch (e) {
      if (!automatic) showStatus("error", "批量采集失败: " + (e as Error).message)
      return null
    }
  }, [loadTopology, probeAllCapabilities, showStatus])

  const collectDevice = useCallback(async (ip: string): Promise<CollectionResult | null> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api(`/api/collect/${encodeURIComponent(ip)}`, { method: "POST" })
      if (data.result) {
        setCollectionResults((prev) => ({ ...prev, [ip]: data.result }))
        if (data.result.ssh_ok) probeSingleCapability(ip)
      }
      return data.result || null
    } catch (e) {
      showStatus("error", `采集 ${ip} 失败: ` + (e as Error).message)
      return null
    }
  }, [probeSingleCapability, showStatus])

  const discoverDevices = useCallback(async (options: { collectAfter?: boolean; subnet?: string } = {}): Promise<unknown> => {
    const collectAfter = options.collectAfter !== false
    const subnet = (options.subnet ?? discoverSubnet).trim()

    showStatus("loading", "正在扫描管理网段并识别 eNSP 设备...")
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/discover-devices", {
        method: "POST",
        body: JSON.stringify({
          subnet: subnet || subnetState || null,
          replace: true,
          ssh_identify: true,
        }),
      })
      if (data.suspicious) {
        showStatus("error", data.message || "发现结果异常偏多，已保留当前设备列表")
        return data
      }
      setDevices(data.devices || [])
      setCollectionResults((prev) => {
        const next = { ...prev }
        for (const result of Object.values(data.discovery_results || {})) {
          next[(result as CollectionResult).ip!] = result as CollectionResult
        }
        return next
      })

      const sshOk = Object.values(data.discovery_results || {}).filter((r) => (r as CollectionResult).ssh_ok).length
      const ignoredCount = (data.ignored || []).length
      const ignoredText = ignoredCount ? `，已忽略 ${ignoredCount} 个未通过 Ping+SSH 的地址` : ""
      if (data.discovered.length && collectAfter) {
        showStatus("loading", `发现 ${data.discovered.length} 台 Ping+SSH 通过的设备，正在自动采集设备信息${ignoredText}...`)
        await collectAll({ automatic: true })
      } else {
        let message = data.discovered.length
          ? `发现 ${data.discovered.length} 台 Ping+SSH 通过的设备，${sshOk} 台完成 SSH 识别${ignoredText}，设备列表已同步`
          : data.replaced
            ? `未发现 Ping+SSH 均通过的 eNSP 设备${ignoredText}，设备列表已同步为空。请检查 Cloud 绑定、管理 IP 和 SSH`
            : `未发现 Ping+SSH 均通过的 eNSP 设备${ignoredText}，已保留当前设备列表。请检查 Cloud 绑定、管理 IP 和 SSH`
        if (!data.discovered.length) {
          message += await getConsoleDiagnosticMessage()
        }
        showStatus(data.discovered.length ? "success" : "error", message)
      }
      return data
    } catch (e) {
      showStatus("error", "发现设备失败: " + (e as Error).message)
      return null
    }
  }, [collectAll, discoverSubnet, getConsoleDiagnosticMessage, showStatus, subnetState])

  // ---------- 启动初始化 ----------
  const loadCollectionResults = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/collection-results")
      setCollectionResults(data.results || {})
    } catch (e) {
      console.error("加载采集结果失败:", (e as Error).message)
    }
  }, [])

  const loadCapabilityResults = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/capability-results")
      setCapabilityResults(data.results || {})
    } catch (e) {
      console.error("加载能力识别结果失败:", (e as Error).message)
    }
  }, [])

  const loadServerData = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/server-data")
      if (!data.success) return
      if (data.selected_interface) {
        setSelectedInterface({ name: data.selected_interface.name, ip: data.selected_interface.ip })
        setSubnetState(data.subnet || "")
      }
      setDevices(data.devices || [])
      setCollectionResults(data.collection_results || {})
      setCapabilityResults(data.capability_results || {})
      loadInterfaces()
      loadTopology(true)
      flashStatus(null)
    } catch (e) {
      console.error("加载服务器数据失败:", (e as Error).message)
      showStatus("error", "加载数据失败: " + (e as Error).message)
    }
  }, [flashStatus, loadInterfaces, loadTopology, showStatus])

  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | null = null
    let cancelled = false
    ;(async () => {
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const data: any = await api("/api/init-status")
        if (cancelled) return
        if (data.initialized) {
          loadServerData()
          return
        }
        showStatus("loading", data.message || (data.in_progress ? "正在初始化..." : "首次启动，正在自动初始化..."))
        if (!data.in_progress) await api("/api/trigger-init", { method: "POST" })
        interval = setInterval(async () => {
          try {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const d: any = await api("/api/init-status")
            if (cancelled) return
            showStatus("loading", d.message || "正在初始化...")
            if (d.initialized) {
              clearInterval(interval!)
              showStatus("success", "初始化完成，正在加载数据...")
              setTimeout(() => { if (!cancelled) loadServerData() }, 500)
            }
          } catch (e) { /* 轮询异常，等下一轮 */ }
        }, 2000)
      } catch (e) {
        console.error("检查初始化状态失败:", e)
        loadServerData()
      }
    })()
    return () => { cancelled = true; if (interval) clearInterval(interval) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 设备侧 ping（AI 助教连通性验证）
  const devicePing = useCallback(async (src: string, dst: string, count = 4): Promise<DevicePingResult> => {
    return api<DevicePingResult>(`/api/devices/${encodeURIComponent(src)}/device-ping`, {
      method: "POST",
      body: JSON.stringify({ target: dst, count }),
    })
  }, [])

  const value: AppContext = {
    section, setSection,
    interfaces, selectedInterface, subnet: subnetState, discoverSubnet, setDiscoverSubnet,
    selectInterface, loadInterfaces,
    devices, collectionResults, capabilityResults, roleResults, topologyLinks,
    loadDevices, saveDevice, deleteDevice, pingDevice,
    discoverDevices, collectDevice, collectAll,
    probeAllCapabilities, probeSingleCapability, analyzeRoles,
    loadTopology, loadServerData, loadCollectionResults, loadCapabilityResults,
    batchStatus, showStatus, flashStatus,
    lastDeploy, setLastDeploy,
    aiConfigured, setAiConfigured,
    aiDrawerOpen, setAiDrawerOpen,
    pendingFill, setPendingFill, pendingAppend, setPendingAppend,
    deployBus, aiBus, workbenchBus,
    devicePing,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
