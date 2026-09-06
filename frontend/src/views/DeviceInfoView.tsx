import React, { useState } from "react"
import { Badge, Button, Card, Group, Paper, Stack, Text, Title, UnstyledButton } from "@mantine/core"
import { IconChevronDown, IconChevronRight, IconRefresh } from "@tabler/icons-react"
import { PROTOCOL_NAMES } from "../constants"
import type { CollectionResult } from "../types"
import { useApp } from "../store"
import { EmptyHint, MonoPre, OnlineBadge } from "../ui"

export function DeviceInfoView() {
  const { collectionResults, loadCollectionResults } = useApp()
  const ips = Object.keys(collectionResults)

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Group gap="sm">
          <Title order={3}>设备信息详情</Title>
          {ips.length > 0 && <Badge variant="light" size="sm">{ips.length} 台设备</Badge>}
        </Group>
        <Button leftSection={<IconRefresh size={14} />} variant="default" size="xs" onClick={loadCollectionResults}>
          刷新采集数据
        </Button>
      </Group>

      {ips.length === 0 && <EmptyHint>暂无采集数据，请先执行设备采集</EmptyHint>}
      <Stack gap="sm">
        {ips.map((ip) => <DeviceCard key={ip} ip={ip} result={collectionResults[ip]} />)}
      </Stack>
    </Paper>
  )
}

function DeviceCard({ ip, result }: { ip: string; result: CollectionResult }) {
  const [open, setOpen] = useState(false)
  const [sectionsOpen, setSectionsOpen] = useState(false)
  const info = result.info || {}
  const commands = result.commands || {}
  const deviceName = info.hostname || result.name || ip
  const protocols = info.protocols || []
  const configSections = Object.entries(info.config_sections || {})

  return (
    <Card withBorder radius="md" padding="sm">
      <UnstyledButton onClick={() => setOpen(!open)} w="100%">
        <Group justify="space-between" wrap="nowrap">
          <Stack gap={4} align="flex-start">
            <Text fw={600} size="sm">{deviceName} ({ip})</Text>
            <Group gap={6} wrap="wrap">
              <Text size="xs" c="dimmed">{info.model || "未知型号"}</Text>
              <Text size="xs" c="dimmed">v{info.version || "-"}</Text>
              <Text size="xs" c="dimmed">CPU: {info.cpu_usage || "-"}</Text>
              <Text size="xs" c="dimmed">MEM: {info.memory_usage || "-"}</Text>
              <OnlineBadge ok={result.online} />
              <OnlineBadge ok={result.ssh_ok} okText="SSH 已连接" failText={`SSH ${result.ssh_error || "失败"}`} unknownText="SSH 未采集" />
              {protocols.length
                ? protocols.map((p: string) => (
                  <Badge key={p} color="blue" variant="light" size="sm" title={`${(PROTOCOL_NAMES as Record<string, string>)[p] || p} 已配置`}>
                    {(PROTOCOL_NAMES as Record<string, string>)[p] || p} 已配置
                  </Badge>
                ))
                : <Badge color="gray" variant="light" size="sm">未检测到协议配置</Badge>}
            </Group>
          </Stack>
          {open ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
        </Group>
      </UnstyledButton>

      {open && (
        <Stack gap="sm" mt="sm">
          {configSections.length > 0 && (
            <div>
              <UnstyledButton onClick={() => setSectionsOpen(!sectionsOpen)}
                style={{ display: "flex", alignItems: "center", gap: 4 }}>
                {sectionsOpen ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
                <Text size="sm" fw={500}>协议配置详情（{configSections.length} 项）</Text>
              </UnstyledButton>
              {sectionsOpen && (
                <Stack gap="sm" mt="sm">
                  {configSections.map(([key, sec]) => (
                    <div key={key}>
                      <Text size="sm" fw={500} mb={4}>{sec.name || key}</Text>
                      <MonoPre>{sec.config || ""}</MonoPre>
                    </div>
                  ))}
                </Stack>
              )}
            </div>
          )}
          {Object.entries(commands).map(([cmd, cmdData]) => (
            <div key={cmd}>
              <Group gap={6} mb={4}>
                <Text size="sm" fw={500}>{cmd}</Text>
                <Badge color={cmdData.success ? "teal" : "red"} variant="light" size="sm">
                  {cmdData.success ? "成功" : "失败"}
                </Badge>
              </Group>
              {cmdData.success
                ? <MonoPre>{cmdData.output}</MonoPre>
                : <Text size="sm" c="red">执行失败: {cmdData.error || "未知错误"}</Text>}
            </div>
          ))}
        </Stack>
      )}
    </Card>
  )
}
