import React, { Suspense, lazy, useEffect, useState } from "react"
import {
  AppShell, Burger, Button, Group, Loader, Menu, Modal, PasswordInput, Text, UnstyledButton,
} from "@mantine/core"
import { useMantineColorScheme } from "@mantine/core"
import type { MantineColorScheme } from "@mantine/core"
import {
  IconDeviceDesktopAnalytics, IconHierarchy, IconListDetails, IconMoon, IconNetwork,
  IconPalette, IconSearch, IconSettings, IconSitemap, IconSun, IconTerminal2,
} from "@tabler/icons-react"
import { AppProvider, useApp } from "./store"
import type { PendingFill } from "./store"
import { ConfirmProvider } from "./ui"
import { getToken, resolveAccessToken, setStoredToken } from "./api"
import { SKINS, setSkin, useSkin } from "./skins"
import { AIDrawer } from "./components/AIDrawer"

// 视图按需加载：首次访问对应分区时才拉取对应 chunk
const Views: Record<string, React.LazyExoticComponent<React.ComponentType>> = {
  "section-interfaces": lazy(() => import("./views/InterfacesView").then((m) => ({ default: m.InterfacesView }))),
  "section-devices": lazy(() => import("./views/DevicesView").then((m) => ({ default: m.DevicesView }))),
  "section-device-info": lazy(() => import("./views/DeviceInfoView").then((m) => ({ default: m.DeviceInfoView }))),
  "section-topology": lazy(() => import("./views/TopologyView").then((m) => ({ default: m.TopologyView }))),
  "section-capabilities": lazy(() => import("./views/CapabilitiesView").then((m) => ({ default: m.CapabilitiesView }))),
  "section-protocol-roles": lazy(() => import("./views/RolesView").then((m) => ({ default: m.RolesView }))),
  "section-config-deploy": lazy(() => import("./views/ConfigDeployView").then((m) => ({ default: m.ConfigDeployView }))),
  "section-free-workbench": lazy(() => import("./views/WorkbenchView").then((m) => ({ default: m.WorkbenchView }))),
}

const SECTIONS = [
  { id: "section-interfaces", label: "本机网卡", icon: IconNetwork },
  { id: "section-devices", label: "设备列表", icon: IconListDetails },
  { id: "section-device-info", label: "设备详情", icon: IconDeviceDesktopAnalytics },
  { id: "section-topology", label: "网络拓扑", icon: IconSitemap },
  { id: "section-capabilities", label: "能力识别", icon: IconSearch },
  { id: "section-protocol-roles", label: "协议角色", icon: IconHierarchy },
  { id: "section-config-deploy", label: "配置下发", icon: IconSettings },
  { id: "section-free-workbench", label: "配置工作台", icon: IconTerminal2 },
]

export default function App() {
  return (
    <AppProvider>
      <ConfirmProvider>
        <Shell />
      </ConfirmProvider>
    </AppProvider>
  )
}

function Shell() {
  const { section, setSection, setAiDrawerOpen, showStatus, pendingFill, setPendingFill, pendingAppend, setPendingAppend } = useApp()
  const { colorScheme, setColorScheme } = useMantineColorScheme()
  // 已访问过的分区才保持挂载（懒加载 + 保留各视图状态）
  const [visited, setVisited] = useState<Set<string>>(() => new Set(["section-interfaces"]))
  useEffect(() => {
    setVisited((prev) => (prev.has(section) ? prev : new Set([...prev, section])))
  }, [section])
  const skin = useSkin()
  const [navOpen, setNavOpen] = useState(false) // 移动端
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("ensp-sidebar-collapsed") === "true")

  useEffect(() => {
    localStorage.setItem("ensp-sidebar-collapsed", String(collapsed))
  }, [collapsed])

  // AI 代码块 → 配置工作台（工作台可能尚未挂载，走待处理队列）
  useEffect(() => {
    const handler = (e: Event) => {
      setPendingAppend((e as CustomEvent).detail || "")
      setSection("section-free-workbench")
      showStatus("success", "AI 建议命令已填入配置工作台，请检查变量后预览下发")
    }
    window.addEventListener("ensp:ai-to-workbench", handler)
    return () => window.removeEventListener("ensp:ai-to-workbench", handler)
  }, [setSection, showStatus, setPendingAppend])

  const cycleTheme = () => {
    const order: MantineColorScheme[] = ["light", "dark", "auto"]
    const next = order[(order.indexOf(colorScheme) + 1) % order.length]
    setColorScheme(next)
  }

  // 深色科技预设自带深色配色；切走时恢复浅色
  const pickSkin = (id: string) => {
    const prev = skin
    setSkin(id)
    if (id === "tech") setColorScheme("dark")
    else if (prev === "tech") setColorScheme("light")
  }

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{
        width: 200,
        breakpoint: "sm",
        collapsed: { mobile: !navOpen, desktop: collapsed },
      }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group wrap="nowrap">
            <Burger opened={navOpen} onClick={() => setNavOpen(!navOpen)} hiddenFrom="sm" size="sm" />
            <Group gap={8} wrap="nowrap">
              <Text size="xl" fw={700} style={{ whiteSpace: "nowrap" }}>eNSP 管理系统</Text>
              <Text size="xs" c="dimmed" visibleFrom="md">网络设备管理平台</Text>
            </Group>
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Menu shadow="md" width={240} position="bottom-end">
              <Menu.Target>
                <Button size="compact-sm" variant="default" leftSection={<IconPalette size={14} />}>
                  {SKINS.find((s) => s.id === skin)?.label || "外观"}
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>界面外观</Menu.Label>
                {SKINS.map((s) => (
                  <Menu.Item key={s.id} onClick={() => pickSkin(s.id)}
                    leftSection={<span style={{ width: 12, height: 12, borderRadius: 6, background: s.accent, display: "inline-block", boxShadow: "0 0 0 2px rgba(0,0,0,0.06)" }} />}
                    rightSection={skin === s.id ? <Text size="xs" c="teal">✓</Text> : null}>
                    <Text size="sm">{s.label}</Text>
                    <Text size="xs" c="dimmed">{s.desc}</Text>
                  </Menu.Item>
                ))}
              </Menu.Dropdown>
            </Menu>
            <Button size="compact-sm" variant="default" onClick={cycleTheme}
              leftSection={colorScheme === "dark" ? <IconMoon size={14} /> : colorScheme === "auto" ? <IconDeviceDesktopAnalytics size={14} /> : <IconSun size={14} />}>
              {colorScheme === "dark" ? "深色模式" : colorScheme === "auto" ? "跟随系统" : "浅色模式"}
            </Button>
            <Button size="compact-sm" variant="light" component="a" href="/legacy">旧版界面</Button>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar>
        <AppShell.Section grow p="xs">
          {SECTIONS.map(({ id, label, icon: Icon }: { id: string; label: string; icon: React.ElementType }) => (
            <UnstyledButton key={id}
              onClick={() => { setSection(id); setNavOpen(false) }}
              w="100%" p="xs" my={1}
              style={{
                display: "flex", alignItems: "center", gap: 8, borderRadius: 8,
                background: section === id ? "var(--mantine-color-blue-light)" : undefined,
                color: section === id ? "var(--mantine-color-blue-light-color)" : undefined,
              }}>
              <Icon size={16} />
              <Text size="sm" style={{ whiteSpace: "nowrap" }}>{label}</Text>
            </UnstyledButton>
          ))}
        </AppShell.Section>
        <AppShell.Section p="xs" visibleFrom="sm">
          <Button size="compact-xs" variant="subtle" w="100%" onClick={() => setCollapsed(!collapsed)}>
            {collapsed ? "»" : "« 收起"}
          </Button>
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main>
        {/* 首次访问的分区懒加载挂载，之后仅切换可见性（保留各视图状态） */}
        {SECTIONS.map(({ id }: { id: string }) => {
          if (!visited.has(id)) return null
          const View = Views[id]
          return (
            <div key={id} style={{ display: section === id ? "block" : "none" }}>
              <Suspense fallback={<Loader style={{ margin: "40px auto", display: "block" }} />}>
                <View />
              </Suspense>
            </div>
          )
        })}
        {pendingFill && <PendingFillBridge pending={pendingFill} onDone={() => setPendingFill(null)} />}
        {pendingAppend && <PendingAppendBridge text={pendingAppend} onDone={() => setPendingAppend(null)} />}

        <Text size="xs" c="dimmed" ta="center" py="lg">
          eNSP Web 网络设备管理系统 · React + Mantine · 后端需运行在安装 eNSP 的 Windows 主机上
        </Text>
      </AppShell.Main>

      {/* AI 悬浮球 */}
      <Button
        onClick={() => setAiDrawerOpen(true)}
        style={{ position: "fixed", right: 22, bottom: 22, zIndex: 200, borderRadius: 999, height: 46, padding: "0 18px" }}
        leftSection={<span style={{ fontSize: 18 }}>🤖</span>}
      >
        AI 助教
      </Button>

      <TokenModal />
      <QuickStartModal />
      <AIDrawer />
    </AppShell>
  )
}

// ---------- 跨视图跳转桥：等目标视图挂载注册总线后执行 ----------
function PendingFillBridge({ pending, onDone }: { pending: PendingFill; onDone: () => void }) {
  const { deployBus } = useApp()
  useEffect(() => {
    if (deployBus.current?.fillInConfig) {
      deployBus.current.fillInConfig(pending.ip, pending.configKey, pending.protocolKey)
      onDone()
    }
  }, [pending, onDone, deployBus])
  return null
}

function PendingAppendBridge({ text, onDone }: { text: string; onDone: () => void }) {
  const { workbenchBus } = useApp()
  useEffect(() => {
    if (workbenchBus.current?.appendCommands) {
      workbenchBus.current.appendCommands(text)
      onDone()
    }
  }, [text, onDone, workbenchBus])
  return null
}

// ---------- 访问令牌弹窗（api.js 401 时触发） ----------
function TokenModal() {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState("")
  const [error, setError] = useState("")

  useEffect(() => {
    const handler = () => {
      setValue(getToken())
      setError("")
      setOpen(true)
    }
    window.addEventListener("ensp:token-required", handler)
    return () => window.removeEventListener("ensp:token-required", handler)
  }, [])

  const submit = () => {
    const v = value.trim()
    if (!v) {
      setError("令牌不能为空：请输入服务端启动日志中打印的令牌（也可查看服务端项目根目录的 web_access.json）")
      return
    }
    setStoredToken(v)
    setOpen(false)
    resolveAccessToken(v)
  }

  return (
    <Modal opened={open} onClose={() => { setOpen(false); resolveAccessToken("") }} title="访问令牌" centered size="sm">
      <Text size="sm" mb="sm">
        本系统可控制网络设备，需要访问令牌。请向教师获取；服务端启动日志会打印令牌
        （也可查看服务端项目根目录的 web_access.json）。
      </Text>
      <PasswordInput
        placeholder="粘贴访问令牌"
        value={value}
        onChange={(e) => setValue(e.currentTarget.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit() }}
        data-autofocus
      />
      {error && <Text size="sm" c="red" mt={8}>{error}</Text>}
      <Group justify="flex-end" mt="lg">
        <Button onClick={submit}>确认</Button>
      </Group>
    </Modal>
  )
}

// ---------- 首次访问快速开始向导 ----------
function QuickStartModal() {
  const [open, setOpen] = useState(() => !localStorage.getItem("quickstart_done"))
  const { setSection, setAiDrawerOpen } = useApp()

  const dismiss = () => {
    localStorage.setItem("quickstart_done", "1")
    setOpen(false)
  }

  const go = (target: string) => {
    dismiss()
    if (target === "ai") setAiDrawerOpen(true)
    else setSection(target)
  }

  const steps: [string, string, string][] = [
    ["识别网卡", "选择与设备同网段的管理网卡", "section-interfaces"],
    ["发现设备", "扫描网段，Ping + SSH 双重探测入库", "section-devices"],
    ["能力识别", "并行探测每台设备支持的协议（三态）", "section-capabilities"],
    ["协议角色", "查看设备在各协议中的角色，点\"填空配置\"直接进预填表单", "section-protocol-roles"],
    ["AI 助教", "基于设备实测数据答疑、诊断报错、指导实验", "ai"],
  ]

  return (
    <Modal opened={open} onClose={dismiss} title="👋 欢迎使用 eNSP 网络设备管理系统" size="md">
      <ol style={{ paddingLeft: 20, lineHeight: 2.2 }}>
        {steps.map(([title, desc, target]: [string, string, string]) => (
          <li key={target}>
            <b>{title}</b>：{desc}{" "}
            <Button size="compact-xs" variant="default" ml={6} onClick={() => go(target)}>
              {target === "ai" ? "打开" : "前往"}
            </Button>
          </li>
        ))}
      </ol>
      <Text size="xs" c="dimmed" mt="sm">
        遇到问题随时在 AI 助教里提问；首次调用接口时需输入服务端打印的访问令牌。
      </Text>
      <Group justify="flex-end" mt="lg">
        <Button onClick={dismiss}>开始使用（不再显示）</Button>
      </Group>
    </Modal>
  )
}
