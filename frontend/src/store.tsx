import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import { api } from "./api"
import type {
  CapabilityResult, CapabilityResult as CapResultT, CollectionResult, DeviceInfo,
  DevicePingResult, LastDeploy, RoleResult, TopologyLink, TopologySummary,
} from "./types"

export type StatusType = "info" | "success" | "error" | "warning" | "loading"

export interface BatchStatus {
  type: StatusType
  message: string
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
  devices: DeviceInfo[]
  devicesRevision: number
  links: TopologyLink[]
  topologies: TopologySummary[]
  workspaceRoot: string
  activeTopology: string | null
  loadLinks: () => Promise<void>
  addLink: (a: { device: string; interface: string }, b: { device: string; interface: string }) => Promise<void>
  removeLink: (a: { device: string; interface: string }, b: { device: string; interface: string }) => Promise<void>
  loadTopologyLibrary: () => Promise<void>
  activateTopology: (name: string) => Promise<void>
  createTopology: (name: string) => Promise<void>
  deleteTopology: (name: string) => Promise<void>
  setWorkspaceRoot: (path: string) => Promise<void>
  collectionResults: Record<string, CollectionResult>
  capabilityResults: Record<string, CapResultT>
  roleResults: Record<string, RoleResult>
  roleNotProbed: { ip: string; name: string }[]
  loadDevices: () => Promise<void>
  saveDevice: (payload: DeviceInfo, editingIp: string | null) => Promise<void>
  deleteDevice: (ip: string) => Promise<void>
  pingDevice: (ip: string) => Promise<{ online?: boolean }>
  collectDevice: (ip: string) => Promise<CollectionResult | null>
  collectAll: (options?: { automatic?: boolean }) => Promise<unknown>
  probeAllCapabilities: (auto?: boolean) => Promise<unknown>
  probeSingleCapability: (ip: string) => Promise<void>
  analyzeRoles: (silent?: boolean) => Promise<Record<string, RoleResult>>
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
 * - 设备/采集/能力/角色 数据与加载动作（级联链：
 *   批量采集 → 自动能力识别 → 静默角色分析）
 * - 批量状态条 batchStatus、最近下发结果 lastDeploy（AI 诊断数据源）
 * - 跨视图协作总线 deployBus / aiBus / workbenchBus
 */
export function AppProvider({ children }: { children: React.ReactNode }) {
  const [section, setSection] = useState("section-devices")

  // 设备与结果
  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [devicesRevision, setDevicesRevision] = useState(0)
  const [links, setLinks] = useState<TopologyLink[]>([])
  const [topologies, setTopologies] = useState<TopologySummary[]>([])
  const [workspaceRoot, setWorkspaceRootState] = useState("")
  const [activeTopology, setActiveTopology] = useState<string | null>(null)
  const [collectionResults, setCollectionResults] = useState<Record<string, CollectionResult>>({})
  const [capabilityResults, setCapabilityResults] = useState<Record<string, CapResultT>>({})
  const [roleResults, setRoleResults] = useState<Record<string, RoleResult>>({})
  const [roleNotProbed, setRoleNotProbed] = useState<{ ip: string; name: string }[]>([])

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

  // ---------- 设备 ----------
  const loadDevices = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/devices")
      setDevices(data.devices || [])
      setDevicesRevision(data.revision ?? 0)
    } catch (e) {
      console.error("加载设备列表失败:", (e as Error).message)
    }
  }, [])

  const loadLinks = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/topology/links")
      setLinks(data.links || [])
    } catch (e) {
      console.error("加载连线失败:", (e as Error).message)
    }
  }, [])

  const addLink = useCallback(async (a: { device: string; interface: string }, b: { device: string; interface: string }) => {
    const data = await api<TopologyLink[]>("/api/topology/links", {
      method: "POST", body: JSON.stringify({ a, b }),
    })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setLinks((data as any).links || [])
  }, [])

  const removeLink = useCallback(async (a: { device: string; interface: string }, b: { device: string; interface: string }) => {
    const qs = new URLSearchParams({
      a_device: a.device, a_interface: a.interface,
      b_device: b.device, b_interface: b.interface,
    })
    const data = await api<TopologyLink[]>(`/api/topology/links?${qs}`, { method: "DELETE" })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setLinks((data as any).links || [])
  }, [])

  const loadTopologyLibrary = useCallback(async () => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/topology/library")
      setTopologies(data.topologies || [])
      setWorkspaceRootState(data.workspace_root || "")
      setActiveTopology(data.active || null)
    } catch (e) {
      console.error("加载拓扑库失败:", (e as Error).message)
    }
  }, [])

  const activateTopology = useCallback(async (name: string) => {
    await api("/api/topology/library/activate", { method: "POST", body: JSON.stringify({ name }) })
    await loadTopologyLibrary()
    await loadDevices()
    await loadLinks()
  }, [loadDevices, loadLinks, loadTopologyLibrary])

  const createTopology = useCallback(async (name: string) => {
    await api("/api/topology/library/create", { method: "POST", body: JSON.stringify({ name }) })
    await loadTopologyLibrary()
  }, [loadTopologyLibrary])

  const deleteTopology = useCallback(async (name: string) => {
    await api(`/api/topology/library/${encodeURIComponent(name)}`, { method: "DELETE" })
    await loadTopologyLibrary()
    await loadDevices()
    await loadLinks()
  }, [loadDevices, loadLinks, loadTopologyLibrary])

  const setWorkspaceRoot = useCallback(async (path: string) => {
    await api("/api/workspace/root", { method: "POST", body: JSON.stringify({ path }) })
    await loadTopologyLibrary()
    await loadDevices()
    await loadLinks()
  }, [loadDevices, loadLinks, loadTopologyLibrary])

  const saveDevice = useCallback(async (payload: DeviceInfo, editingIp: string | null) => {
    // 带上当前版本号，后端做乐观锁校验（过期则 409，避免覆盖别处的改动）
    const body = JSON.stringify({ ...payload, revision: devicesRevision })
    if (editingIp) {
      await api(`/api/devices/${encodeURIComponent(editingIp)}`, { method: "PUT", body })
    } else {
      await api("/api/devices", { method: "POST", body })
    }
    await loadDevices()
  }, [loadDevices, devicesRevision])

  const deleteDevice = useCallback(async (ip: string) => {
    await api(`/api/devices/${encodeURIComponent(ip)}?revision=${devicesRevision}`, { method: "DELETE" })
    setCollectionResults((prev) => {
      const next = { ...prev }
      delete next[ip]
      return next
    })
    await loadDevices()
  }, [loadDevices, devicesRevision])

  const pingDevice = useCallback(async (ip: string) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const data: any = await api("/api/ping", { method: "POST", body: JSON.stringify({ ip }) })
    setCollectionResults((prev) => ({ ...prev, [ip]: { ...(prev[ip] || {}), online: !!data.online } }))
    return data
  }, [])

  // ---------- 采集与分析（级联链） ----------
  const analyzeRoles = useCallback(async (silent = false): Promise<Record<string, RoleResult>> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/protocol-roles-all", { method: "POST" })
      const map: Record<string, RoleResult> = {}
      for (const r of data.results || []) map[r.ip] = r
      setRoleResults(map)
      // 后端会区分"探测过但没角色"和"从未探测过"；后者要提示去能力识别，
      // 否则界面把这些设备渲染成空壳卡片，看不出差别。
      setRoleNotProbed(data.not_probed || [])
      if (!silent && data.count === 0 && !(data.not_probed || []).length) {
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
          `采集及能力识别完成: ${(data.results || []).filter((r: CapResultT) => r.ok).length}/${(data.results || []).length} 台设备识别成功`)
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

  const collectAll = useCallback(async (options: { automatic?: boolean } = {}): Promise<unknown> => {
    const automatic = !!options.automatic
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api("/api/collect-all", { method: "POST" })
      if (data.devices) setDevices(data.devices)
      // 后端 /api/collect-all 返回的是**数组**，必须按 ip 建索引后再合并。
      // 直接展开数组会得到 {0:…,1:…} 这类下标键，collectionResults[ip] 永远查不到，
      // 表格里的运行/采集状态就会停在挂载时的旧值（横幅却因为走 Object.values 而是对的）。
      const byIp: Record<string, CollectionResult> = {}
      for (const r of (data.results || []) as CollectionResult[]) {
        if (r?.ip) byIp[r.ip] = r
      }
      setCollectionResults((prev) => ({ ...prev, ...byIp }))
      const results: CollectionResult[] = Object.values(byIp)
      const okCount = results.filter((r) => r.ok).length
      if (!automatic) {
        showStatus("success", `批量采集完成: ${okCount}/${results.length} 台成功`)
      }
      if (okCount > 0) await probeAllCapabilities(true)
      return data
    } catch (e) {
      if (!automatic) showStatus("error", "批量采集失败: " + (e as Error).message)
      return null
    }
  }, [probeAllCapabilities, showStatus])

  const collectDevice = useCallback(async (ip: string): Promise<CollectionResult | null> => {
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const data: any = await api(`/api/collect/${encodeURIComponent(ip)}`, { method: "POST" })
      if (data.result) {
        setCollectionResults((prev) => ({ ...prev, [ip]: data.result }))
        if (data.result.ok) probeSingleCapability(ip)
      }
      return data.result || null
    } catch (e) {
      showStatus("error", `采集 ${ip} 失败: ` + (e as Error).message)
      return null
    }
  }, [probeSingleCapability, showStatus])

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
      setDevices(data.devices || [])
      setCollectionResults(data.collection_results || {})
      setCapabilityResults(data.capability_results || {})
      flashStatus(null)
    } catch (e) {
      console.error("加载服务器数据失败:", (e as Error).message)
      showStatus("error", "加载数据失败: " + (e as Error).message)
    }
  }, [flashStatus, showStatus])

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
    devices, devicesRevision, collectionResults, capabilityResults, roleResults, roleNotProbed,
    links, topologies, workspaceRoot, activeTopology,
    loadLinks, addLink, removeLink,
    loadTopologyLibrary, activateTopology, createTopology, deleteTopology, setWorkspaceRoot,
    loadDevices, saveDevice, deleteDevice, pingDevice,
    collectDevice, collectAll,
    probeAllCapabilities, probeSingleCapability, analyzeRoles,
    loadServerData, loadCollectionResults, loadCapabilityResults,
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
