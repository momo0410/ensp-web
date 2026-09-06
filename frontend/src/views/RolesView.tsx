import React, { useState } from "react"
import { Alert, Badge, Button, Card, Group, Paper, Stack, Text, Title, UnstyledButton } from "@mantine/core"
import { IconChevronDown, IconChevronRight, IconSearch } from "@tabler/icons-react"
import { ROLE_NAMES } from "../constants"
import type { RoleResult } from "../types"
import { useApp } from "../store"
import { CapBadge, EmptyHint } from "../ui"

const STATUS_BADGES: Record<string, React.ReactNode> = {
  running: <CapBadge kind="running">运行中</CapBadge>,
  configured: <CapBadge kind="configured">已配置</CapBadge>,
  supported: <CapBadge kind="supported">未配置</CapBadge>,
}

export function RolesView() {
  const { roleResults, analyzeRoles, setSection, setPendingFill } = useApp()
  const [analyzing, setAnalyzing] = useState(false)
  const results = Object.values(roleResults)

  const analyze = async () => {
    setAnalyzing(true)
    try { await analyzeRoles() } finally { setAnalyzing(false) }
  }

  const fillConfig = (ip: string, configKey: string, protocolKey: string) => {
    setPendingFill({ ip, configKey, protocolKey })
    setSection("section-config-deploy")
  }

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3}>协议角色总览</Title>
        <Button leftSection={<IconSearch size={14} />} loading={analyzing} onClick={analyze}>
          分析协议角色
        </Button>
      </Group>

      <Alert variant="light" color="blue" mb="md">
        展示每台设备在各协议中的角色（VRRP Master/Backup、STP 根桥、DHCP Server/中继、NAT 类型……），
        并提供"填空式"配置入口：点"填空配置"打开表单，系统已按设备现状预填接口/IP 等可推导项，补齐空格即可下发。
        需先完成能力识别。
      </Alert>

      {results.length === 0 && <EmptyHint>暂无角色分析数据，请先执行能力识别，再点击"分析协议角色"</EmptyHint>}
      <Stack gap="sm">
        {results.map((r) => <RoleCard key={r.ip} r={r} onFill={fillConfig} />)}
      </Stack>
    </Paper>
  )
}

function RoleCard({ r, onFill }: { r: RoleResult; onFill: (ip: string, configKey: string, protocolKey: string) => void }) {
  const [open, setOpen] = useState(false)
  const entries = Object.entries(r.protocols || {})
  const runningCount = entries.filter(([, p]) => p.status === "running").length
  const fillableCount = entries.filter(([, p]) => p.config_key).length
  const role = r.device_role || "unknown"

  return (
    <Card withBorder radius="md" padding="sm">
      <UnstyledButton onClick={() => setOpen(!open)} w="100%">
        <Group justify="space-between" wrap="nowrap">
          <Stack gap={4} align="flex-start">
            <Text fw={600} size="sm">{r.hostname || r.ip} ({r.ip})</Text>
            <Group gap={6} wrap="wrap">
              <Badge variant="light" size="sm" color={role === "router" ? "blue" : role === "switch" ? "cyan" : role === "l3_switch" ? "indigo" : "gray"}>
                {(ROLE_NAMES as Record<string, string>)[role] || role}
              </Badge>
              <Badge color="grape" variant="light" size="sm">运行中 {runningCount}</Badge>
              <Badge color="blue" variant="light" size="sm">可填空配置 {fillableCount}</Badge>
            </Group>
          </Stack>
          {open ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
        </Group>
      </UnstyledButton>

      {open && (
        <Stack gap={8} mt="sm">
          {entries.map(([key, p]) => (
            <Group key={key} justify="space-between" gap="sm" wrap="nowrap" align="center">
              <Text size="sm" w={120} className="cap-name">{p.name}</Text>
              <Group gap={4} wrap="wrap" style={{ flex: 1 }}>
                {(p.roles || []).length
                  ? (p.roles || []).map((rl, i) => (
                    <Badge key={i} variant="outline" size="sm" color="gray"
                      title={`${rl.detail || ""}${rl.interface ? " @" + rl.interface : ""}`}>
                      {rl.role}{rl.interface && <Text component="small" size="9px" ml={2}>@{rl.interface}</Text>}
                    </Badge>
                  ))
                  : <Text size="xs" c="dimmed">未配置，点右侧开始</Text>}
              </Group>
              <Group gap={6} wrap="nowrap">
                {STATUS_BADGES[p.status] || <CapBadge kind="unsupported">未知</CapBadge>}
                {p.config_key && (
                  <Button size="xs" variant="default"
                    onClick={() => onFill(r.ip, p.config_key || "", key)}>
                    填空配置
                  </Button>
                )}
              </Group>
            </Group>
          ))}
        </Stack>
      )}
    </Card>
  )
}
