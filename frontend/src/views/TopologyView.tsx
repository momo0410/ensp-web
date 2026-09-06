import React from "react"
import { Button, Code, Group, Paper, Table, Title } from "@mantine/core"
import { IconRefresh } from "@tabler/icons-react"
import { useApp } from "../store"
import { EmptyHint } from "../ui"

export function TopologyView() {
  const { topologyLinks, loadTopology } = useApp()

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3}>LLDP 拓扑关系</Title>
        <Button leftSection={<IconRefresh size={14} />} variant="default" size="xs" onClick={() => loadTopology()}>
          刷新拓扑
        </Button>
      </Group>

      <Table.ScrollContainer minWidth={800}>
        <Table verticalSpacing="xs" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>本端设备</Table.Th>
              <Table.Th>本端 IP</Table.Th>
              <Table.Th>本端接口</Table.Th>
              <Table.Th>对端设备</Table.Th>
              <Table.Th>对端 IP</Table.Th>
              <Table.Th>对端接口</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {topologyLinks.length === 0 && (
              <Table.Tr><Table.Td colSpan={6}><EmptyHint>暂无拓扑数据，请先采集设备 LLDP 信息</EmptyHint></Table.Td></Table.Tr>
            )}
            {topologyLinks.map((link, i) => (
              <Table.Tr key={i}>
                <Table.Td><b>{link.source}</b></Table.Td>
                <Table.Td>{link.source_ip}</Table.Td>
                <Table.Td><Code>{link.local_interface}</Code></Table.Td>
                <Table.Td><b>{link.target}</b></Table.Td>
                <Table.Td>{link.target_ip || "未知"}</Table.Td>
                <Table.Td><Code>{link.remote_interface}</Code></Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Paper>
  )
}
