import React, { useState } from "react"
import { Badge, Button, Card, Group, Paper, Stack, Text, Title, UnstyledButton } from "@mantine/core"
import { IconChevronDown, IconChevronRight, IconSearch } from "@tabler/icons-react"
import { CAPABILITY_NAMES, ROLE_NAMES } from "../constants"
import { useApp } from "../store"
import { api } from "../api"
import type { CapabilityItem, CapabilityResult, MatrixInfo, ProtocolTreeResult } from "../types"
import { CapBadge, EmptyHint, EvidenceBadge, MonoPre } from "../ui"

export function CapabilitiesView() {
  const { capabilityResults, probeAllCapabilities } = useApp()
  const [probing, setProbing] = useState(false)
  const results = Object.values(capabilityResults)

  const probe = async () => {
    setProbing(true)
    try { await probeAllCapabilities() } finally { setProbing(false) }
  }

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3}>设备能力识别</Title>
        <Button leftSection={<IconSearch size={14} />} loading={probing} onClick={probe}>
          识别所有设备能力
        </Button>
      </Group>

      {results.length === 0 && <EmptyHint>暂无能力识别数据，请点击"识别所有设备能力"</EmptyHint>}
      <Stack gap="sm">
        {results.map((r) => <CapabilityCard key={r.ip} r={r} />)}
      </Stack>
    </Paper>
  )
}

function CapabilityCard({ r }: { r: CapabilityResult }) {
  const [open, setOpen] = useState(false)
  const [tree, setTree] = useState<null | "loading" | { data?: ProtocolTreeResult; error?: string }>(null)
  const role = r.device_role || "unknown"
  const caps = Object.entries(r.capabilities || {})
  const count = (fn: (c: CapabilityItem) => boolean) => caps.filter(([, c]) => fn(c as CapabilityItem)).length

  const discoverTree = async () => {
    setTree("loading")
    try {
      const data = await api(`/api/devices/${encodeURIComponent(r.ip)}/protocol-tree`)
      setTree({ data })
    } catch (e) {
      setTree({ error: (e as Error).message })
    }
  }

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
              <Badge color={r.ok ? "teal" : "red"} variant="light" size="sm">{r.ok ? "已连接" : "连接失败"}</Badge>
              <Badge color="blue" variant="light" size="sm">支持 {count((c) => !!c.supported)}</Badge>
              <Badge color="teal" variant="light" size="sm">已配置 {count((c) => !!c.configured)}</Badge>
              <Badge color="grape" variant="light" size="sm">运行中 {count((c) => !!c.running)}</Badge>
              <MatrixBadge matrix={r.matrix} />
            </Group>
          </Stack>
          {open ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
        </Group>
      </UnstyledButton>

      {open && (
        <Stack gap="sm" mt="sm">
          <Stack gap={4}>
            {caps.map(([key, cap]: [string, CapabilityItem]) => (
              <Group key={key} justify="space-between" gap="xs" wrap="nowrap"
                title={`${cap.display_cmd || ""}${cap.note ? " | " + cap.note : ""}`}>
                <Text size="sm" className="cap-name">{(CAPABILITY_NAMES as Record<string, string>)[key] || key}</Text>
                <Group gap={4} wrap="nowrap">
                  {cap.supported ? (
                    <>
                      <CapBadge kind="supported">支持</CapBadge>
                      {cap.configured && <CapBadge kind="configured">已配置</CapBadge>}
                      {cap.running && <CapBadge kind="running">运行中</CapBadge>}
                    </>
                  ) : (
                    <CapBadge kind="unsupported">不支持</CapBadge>
                  )}
                  <EvidenceBadge cap={cap} />
                </Group>
              </Group>
            ))}
          </Stack>

          {r.ok && (
            <>
              <Group>
                <Button size="xs" variant="default" onClick={discoverTree}>? 命令树协议发现</Button>
              </Group>
              {tree === "loading" && <Text size="sm" c="dimmed">正在采集 ? 命令树（display ? / 系统视图 / 接口视图）...</Text>}
              {tree && tree !== "loading" && tree.error && <Text size="sm" c="red">命令树发现失败: {tree.error}</Text>}
              {tree && tree !== "loading" && tree.data && <ProtocolTree data={tree.data} />}
            </>
          )}

          {(r.errors || []).length > 0 && (
            <Text size="sm" c="red">错误: {(r.errors || []).join("; ")}</Text>
          )}
        </Stack>
      )}
    </Card>
  )
}

function MatrixBadge({ matrix }: { matrix?: MatrixInfo }) {
  if (!matrix) return null
  if (matrix.family) {
    return (
      <CapBadge kind="matrix" title={`eNSP 镜像能力矩阵匹配: ${matrix.family_label || matrix.family}${matrix.image ? ` (${matrix.image})` : ""}`}>
        矩阵 {matrix.family_label || matrix.family}
      </CapBadge>
    )
  }
  if (matrix.image) {
    return <CapBadge kind="matrix" title={`镜像 ${matrix.image} 未收录矩阵，全部以实探为准`}>矩阵未收录</CapBadge>
  }
  return null
}

function ProtocolTree({ data }: { data: ProtocolTreeResult }) {
  if (!data || data.success === false) return <Text size="sm" c="red">命令树发现失败</Text>
  const views = data.views || {}
  const supported = Object.entries(data.protocols || {}).filter(([, p]) => p.supported)
  const meta = []
  if (data.model) meta.push(`型号 ${data.model}`)
  if (data.image) meta.push(`镜像 ${data.image}`)
  if (data.interface) meta.push(`接口视图 ${data.interface}`)
  return (
    <Stack gap={6}>
      <Text size="sm">{meta.length ? meta.join(" | ") + " — " : ""}支持协议 {supported.length} 项</Text>
      <Group gap={4} wrap="wrap">
        {supported.length
          ? supported.map(([k, p]: [string, { name?: string; evidence?: string }]) => (
            <Badge key={k} color="teal" variant="light" size="sm" title={p.evidence || ""}>{p.name || k}</Badge>
          ))
          : <Text size="sm" c="dimmed">未发现支持协议</Text>}
      </Group>
      {Object.entries(views).map(([scope, kws]: [string, string[]]) => {
        const label = { display: "display ?", system: "系统视图 ?", interface: "接口视图 ?" }[scope] || scope
        return (
          <Text key={scope} size="xs">
            <b>{label}</b>（{kws.length} 个关键字）: {kws.slice(0, 60).join("、")}{kws.length > 60 ? " …" : ""}
          </Text>
        )
      })}
    </Stack>
  )
}
