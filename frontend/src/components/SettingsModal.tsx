import { useEffect, useState } from "react"
import {
  Alert, Badge, Button, Group, Modal, Stack, Switch, Text, TextInput,
} from "@mantine/core"
import { useApp } from "../store"
import { api } from "../api"

export interface SettingsSchemaItem {
  key: string
  type: "bool" | "int"
  default: boolean | number
  restart: boolean
  label: string
  desc: string
  min?: number
  max?: number
}

interface SettingsResponse {
  success: boolean
  settings: Record<string, boolean | number>
  schema: SettingsSchemaItem[]
  updated?: Record<string, boolean | number>
  need_restart?: string[]
  message?: string
  error?: string
}

export function SettingsModal({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { showStatus } = useApp()
  const [schema, setSchema] = useState<SettingsSchemaItem[]>([])
  const [values, setValues] = useState<Record<string, boolean | number>>({})
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [needRestart, setNeedRestart] = useState<string[]>([])

  useEffect(() => {
    if (!opened) return
    let alive = true
    api<SettingsResponse>("/api/settings")
      .then((data) => {
        if (!alive) return
        setSchema(data.schema || [])
        setValues(data.settings || {})
        setNotice(null)
        setNeedRestart([])
      })
      .catch(() => alive && setNotice("设置读取失败"))
    return () => { alive = false }
  }, [opened])

  const set = (key: string, value: boolean | number) => {
    setValues((prev) => ({ ...prev, [key]: value }))
  }

  const save = async () => {
    setSaving(true)
    try {
      const changed: Record<string, boolean | number> = {}
      for (const item of schema) {
        const current = values[item.key]
        if (current !== undefined) changed[item.key] = current
      }
      const res = await api<SettingsResponse>("/api/settings", {
        method: "POST",
        body: JSON.stringify(changed),
      })
      setNotice(res.message || "设置已保存")
      setNeedRestart(res.need_restart || [])
      showStatus("success", res.message || "设置已保存")
    } catch (e) {
      setNotice(`保存失败: ${(e as Error).message}`)
      showStatus("error", `保存失败: ${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal opened={opened} onClose={onClose} title="项目设置" size="lg" centered>
      <Stack gap="md">
        <Text size="sm" c="dimmed">
          这些开关控制项目与 eNSP 的联动行为。标注「需重启」的项保存后要重启后端进程才生效。
        </Text>

        {notice && (
          <Alert
            color={needRestart.length ? "yellow" : "teal"}
            title="提示"
            variant="light"
            onClose={() => setNotice(null)}
            withCloseButton
          >
            {notice}
          </Alert>
        )}

        {schema.map((item) => (
          <Group key={item.key} justify="space-between" align="flex-start" wrap="nowrap">
            <div style={{ flex: 1 }}>
              <Group gap={6}>
                <Text size="sm" fw={500}>{item.label}</Text>
                {item.restart && <Badge size="xs" color="yellow" variant="light">需重启</Badge>}
              </Group>
              <Text size="xs" c="dimmed">{item.desc}</Text>
            </div>
            {item.type === "bool" ? (
              <Switch
                checked={Boolean(values[item.key])}
                onChange={(e) => set(item.key, e.currentTarget.checked)}
                aria-label={item.label}
              />
            ) : (
              <TextInput
                type="number"
                w={110}
                value={String(values[item.key] ?? item.default)}
                min={item.min}
                max={item.max}
                onChange={(e) => set(item.key, Number(e.currentTarget.value))}
              />
            )}
          </Group>
        ))}

        <Group justify="flex-end" mt="sm">
          <Button variant="default" onClick={onClose}>关闭</Button>
          <Button loading={saving} onClick={save}>保存</Button>
        </Group>
      </Stack>
    </Modal>
  )
}
