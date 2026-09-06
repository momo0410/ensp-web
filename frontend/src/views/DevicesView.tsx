import React, { useState } from "react"
import {
  Button, Group, NumberInput, Paper, PasswordInput, Select, Stack, Table, Text, TextInput, Title,
} from "@mantine/core"
import { IconDeviceDesktopAnalytics, IconPlus, IconRefresh, IconSearch } from "@tabler/icons-react"
import { useApp } from "../store"
import type { DeviceInfo } from "../types"
import { BatchStatusBanner, OnlineBadge, useConfirm } from "../ui"

export function DevicesView() {
  const {
    devices, collectionResults, discoverSubnet, setDiscoverSubnet, discoverDevices, loadDevices,
    saveDevice, deleteDevice, pingDevice, collectDevice,
  } = useApp()

  const confirm = useConfirm()
  const [formOpen, setFormOpen] = useState(false)
  const [editingIp, setEditingIp] = useState<string | null>(null)
  const [form, setForm] = useState({ name: "", ip: "", username: "admin", password: "Huawei@123", device_type: "huawei", console_port: "" })
  const [scanning, setScanning] = useState(false)
  const [saving, setSaving] = useState(false)

  const openAdd = () => {
    setEditingIp(null)
    setForm({ name: "", ip: "", username: "admin", password: "Huawei@123", device_type: "huawei", console_port: "" })
    setFormOpen(true)
  }

  const openEdit = (device: DeviceInfo) => {
    setEditingIp(device.ip)
    setForm({
      name: device.name || "", ip: device.ip || "",
      username: device.username || "admin", password: device.password || "Huawei@123",
      device_type: device.device_type || "huawei",
      console_port: device.console_port ? String(device.console_port) : "",
    })
    setFormOpen(true)
  }

  const submit = async () => {
    if (!form.name.trim() || !form.ip.trim()) return
    setSaving(true)
    try {
      const port = parseInt(form.console_port, 10)
      const payload: DeviceInfo = {
        ...form,
        console_port: isNaN(port) ? null : port,
      }
      await saveDevice(payload, editingIp)
      setFormOpen(false)
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (device: DeviceInfo) => {
    const ok = await confirm("删除设备", `确定删除设备 ${device.name} (${device.ip})？删除后其采集数据将一并清除。`)
    if (!ok) return
    try { await deleteDevice(device.ip) } catch (e) { alert((e as Error).message) }
  }

  const scan = async () => {
    setScanning(true)
    try { await discoverDevices() } finally { setScanning(false) }
  }

  return (
    <Paper shadow="xs" radius="md" p="lg" withBorder>
      <Group justify="space-between" mb="md">
        <Title order={3}>设备列表</Title>
        <Group gap="xs">
          <Button leftSection={<IconSearch size={14} />} loading={scanning} onClick={scan}>发现 eNSP 设备</Button>
          <Button leftSection={<IconRefresh size={14} />} variant="default" onClick={() => loadDevices()}>刷新列表</Button>
          <Button leftSection={<IconPlus size={14} />} onClick={openAdd}>添加设备</Button>
        </Group>
      </Group>

      <Group align="center" mb="sm" gap="xs">
        <Text size="sm">扫描网段：</Text>
        <TextInput
          w={260} size="xs"
          placeholder="留空使用当前管理网卡网段"
          value={discoverSubnet}
          onChange={(e) => setDiscoverSubnet(e.currentTarget.value)}
        />
      </Group>

      <Stack gap="sm" mb="md">
        <BatchStatusBanner />
      </Stack>

      {formOpen && (
        <Paper withBorder p="md" mb="md" radius="md">
          <Text fw={600} mb="sm">{editingIp ? "编辑设备连接" : "添加设备"}</Text>
          <Stack gap="sm">
            <Group grow>
              <TextInput label="设备名称" placeholder="例如 AR3" value={form.name}
                onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} />
              <TextInput label="管理 IP" placeholder="例如 192.168.100.13" value={form.ip}
                onChange={(e) => setForm({ ...form, ip: e.currentTarget.value })} />
            </Group>
            <Group grow>
              <TextInput label="用户名" value={form.username}
                onChange={(e) => setForm({ ...form, username: e.currentTarget.value })} />
              <PasswordInput label="密码" value={form.password}
                onChange={(e) => setForm({ ...form, password: e.currentTarget.value })} />
            </Group>
            <Group grow>
              <Select label="设备类型" data={[{ value: "huawei", label: "huawei" }, { value: "huawei_vrp", label: "huawei_vrp" }]}
                value={form.device_type} onChange={(v) => setForm({ ...form, device_type: v || "huawei" })} />
              <NumberInput label="Console 端口（可选）" placeholder="例如 2000" value={form.console_port || ""}
                onChange={(v) => setForm({ ...form, console_port: v == null ? "" : String(v) })} />
            </Group>
          </Stack>
          <Group mt="md">
            <Button loading={saving} onClick={submit}>{editingIp ? "保存修改" : "确认添加"}</Button>
            <Button variant="default" onClick={() => setFormOpen(false)}>取消</Button>
          </Group>
        </Paper>
      )}

      <Table.ScrollContainer minWidth={700}>
        <Table verticalSpacing="xs" highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>设备名称</Table.Th>
              <Table.Th>管理 IP</Table.Th>
              <Table.Th>在线状态</Table.Th>
              <Table.Th>SSH 状态</Table.Th>
              <Table.Th>操作</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {devices.length === 0 && (
              <Table.Tr><Table.Td colSpan={5}><Text c="dimmed" size="sm" ta="center" py="lg">暂无设备，请扫描或手动添加</Text></Table.Td></Table.Tr>
            )}
            {devices.map((device) => {
              const result = collectionResults[device.ip] || {}
              return (
                <Table.Tr key={device.ip}>
                  <Table.Td><b>{device.name}</b></Table.Td>
                  <Table.Td>{device.ip}</Table.Td>
                  <Table.Td><OnlineBadge ok={result.online} /></Table.Td>
                  <Table.Td>
                    <OnlineBadge ok={result.ssh_ok} okText="已连接" failText="失败" unknownText="未采集" />
                  </Table.Td>
                  <Table.Td>
                    <Group gap="xs">
                      <Button size="xs" onClick={() => openEdit(device)}>配置</Button>
                      <Button size="xs" variant="default"
                        onClick={async () => {
                          try {
                            const r = await pingDevice(device.ip)
                            if (!r.online) alert(`${device.ip} ping 不通`)
                          } catch (e) { alert((e as Error).message) }
                        }}>
                        Ping
                      </Button>
                      <Button size="xs" variant="default" leftSection={<IconDeviceDesktopAnalytics size={13} />}
                        onClick={() => collectDevice(device.ip)}>
                        采集
                      </Button>
                      <Button size="xs" variant="light" color="red" onClick={() => remove(device)}>删除</Button>
                    </Group>
                  </Table.Td>
                </Table.Tr>
              )
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Paper>
  )
}
