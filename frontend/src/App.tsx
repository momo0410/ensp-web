import React, { Suspense, lazy, useEffect, useState } from "react"
import {
  AppShell, Burger, Button, Group, Loader, Menu, Text, UnstyledButton,
} from "@mantine/core"
import { useMantineColorScheme } from "@mantine/core"
import type { MantineColorScheme } from "@mantine/core"
import {
  IconDeviceDesktopAnalytics, IconHierarchy, IconListDetails, IconMoon,
  IconPalette, IconSearch, IconSettings, IconSun, IconTerminal2,
} from "@tabler/icons-react"
import { AppProvider, useApp } from "./store"
import type { PendingFill } from "./store"
import { ConfirmProvider } from "./ui"
import { SKINS, setSkin, useSkin } from "./skins"
import { AIDrawer } from "./components/AIDrawer"
import { SettingsModal } from "./components/SettingsModal"

// 视图按需加载：首次访问对应分区时才拉取对应 chunk
const Views: Record<string, React.LazyExoticComponent<React.ComponentType>> = {
  "section-devices": lazy(() => import("./views/DevicesView").then((m) => ({ default: m.DevicesView }))),
  "section-device-info": lazy(() => import("./views/DeviceInfoView").then((m) => ({ default: m.DeviceInfoView }))),
  "section-capabilities": lazy(() => import("./views/CapabilitiesView").then((m) => ({ default: m.CapabilitiesView }))),
  "section-protocol-roles": lazy(() => import("./views/RolesView").then((m) => ({ default: m.RolesView }))),
  "section-config-deploy": lazy(() => import("./views/ConfigDeployView").then((m) => ({ default: m.ConfigDeployView }))),
  "section-free-workbench": lazy(() => import("./views/WorkbenchView").then((m) => ({ default: m.WorkbenchView }))),
}

const SECTIONS = [
  { id: "section-devices", label: "设备管理", icon: IconListDetails },
  { id: "section-device-info", label: "设备详情", icon: IconDeviceDesktopAnalytics },
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
  const [visited, setVisited] = useState<Set<string>>(() => new Set(["section-devices"]))
  useEffect(() => {
    setVisited((prev) => (prev.has(section) ? prev : new Set([...prev, section])))
  }, [section])
  const skin = useSkin()
  const [navOpen, setNavOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("ensp-sidebar-collapsed") === "true")

  useEffect(() => {
    localStorage.setItem("ensp-sidebar-collapsed", String(collapsed))
  }, [collapsed])

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
            <Button size="compact-sm" variant="subtle" px={6} visibleFrom="sm"
              aria-label="展开侧边栏" title="展开侧边栏"
              style={{ display: collapsed ? undefined : "none" }}
              onClick={() => setCollapsed(false)}>
              »
            </Button>
            <Group gap={8} wrap="nowrap">
              <Text size="xl" fw={700} style={{ whiteSpace: "nowrap" }}>eNSP 管理系统</Text>
              <Text size="xs" c="dimmed" visibleFrom="md">网络设备管理平台</Text>
            </Group>
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Button size="compact-sm" variant="default" onClick={() => setSettingsOpen(true)}
              leftSection={<IconSettings size={14} />}>
              设置
            </Button>
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
          eNSP Web 网络设备管理系统 · React + Mantine · 本地 Console 直连管控
        </Text>
      </AppShell.Main>

      <Button
        onClick={() => setAiDrawerOpen(true)}
        style={{ position: "fixed", right: 22, bottom: 22, zIndex: 200, borderRadius: 999, height: 46, padding: "0 18px" }}
        leftSection={<span style={{ fontSize: 18 }}>🤖</span>}
      >
        AI 助教
      </Button>

      <AIDrawer />
      <SettingsModal opened={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </AppShell>
  )
}

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
