import React from "react"
import { Alert, Badge, Button, Group, Paper, Stack, Table, Text, Title } from "@mantine/core"
import { IconRefresh } from "@tabler/icons-react"
import { useApp } from "../store"
import { EmptyHint, OnlineBadge } from "../ui"

export function InterfacesView() {
  const { interfaces, selectedInterface, subnet, loadInterfaces, selectInterface } = useApp()

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3}>本机网卡信息</Title>
        <Button leftSection={<IconRefresh size={14} />} variant="default" size="xs" onClick={() => loadInterfaces()}>
          刷新网卡
        </Button>
      </Group>

      {selectedInterface && (
        <Alert color={selectedInterface.auto ? "teal" : "blue"} variant="light" mb="md">
          ✔ 当前管理网卡：<b>{selectedInterface.name}</b> · IP：<b>{selectedInterface.ip}</b> · 网段：<b>{subnet}</b>
          {selectedInterface.auto && "（自动识别）"}
        </Alert>
      )}

      <Table.ScrollContainer minWidth={900}>
        <Table verticalSpacing="xs" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>网卡名称</Table.Th>
              <Table.Th>IPv4</Table.Th>
              <Table.Th>子网掩码</Table.Th>
              <Table.Th>MAC</Table.Th>
              <Table.Th>状态</Table.Th>
              <Table.Th>网段</Table.Th>
              <Table.Th>类型</Table.Th>
              <Table.Th>操作</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {interfaces.length === 0 && (
              <Table.Tr><Table.Td colSpan={8}><EmptyHint>未发现网卡，点击"刷新网卡"加载</EmptyHint></Table.Td></Table.Tr>
            )}
            {interfaces.map((iface) => (
              <Table.Tr key={iface.name} bg={iface.is_recommended ? "var(--mantine-color-teal-light)" : undefined}>
                <Table.Td>
                  <Text size="sm" fw={500}>{iface.name}</Text>
                  {iface.description && <Text size="xs" c="dimmed">{iface.description}</Text>}
                </Table.Td>
                <Table.Td>{iface.ipv4 || "-"}</Table.Td>
                <Table.Td>{iface.netmask || "-"}</Table.Td>
                <Table.Td>{iface.mac || "-"}</Table.Td>
                <Table.Td><OnlineBadge ok={iface.status === "UP"} okText={iface.status} failText={iface.status} /></Table.Td>
                <Table.Td>{iface.subnet || "-"}</Table.Td>
                <Table.Td>
                  {iface.is_recommended ? <Badge color="teal" variant="light" size="sm">推荐</Badge>
                    : iface.is_ensp ? <Badge color="yellow" variant="light" size="sm">疑似 eNSP</Badge>
                      : iface.ipv4 ? <Badge color="blue" variant="light" size="sm">普通</Badge>
                        : "-"}
                  {iface.recommendation_reason && <Text size="xs" c="dimmed">{iface.recommendation_reason}</Text>}
                </Table.Td>
                <Table.Td>
                  <Button size="xs" variant="default" disabled={!iface.ipv4}
                    onClick={() => selectInterface(iface.name, iface.ipv4!, iface.netmask!)}>
                    设为管理网卡
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Paper>
  )
}
