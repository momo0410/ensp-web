import React, { useState } from "react"
import { Button, Checkbox, Group, NumberInput, Paper, Stack, Text, Textarea, Title } from "@mantine/core"
import { IconEye, IconSend } from "@tabler/icons-react"
import { notifications } from "@mantine/notifications"
import { useApp } from "../../store"
import { api } from "../../api"
import { useConfirm, MonoPre } from "../../ui"
import { DeployResult } from "../../components/DeployResult"
import type { DeployOutcome } from "../../types"

// Console 端口直连配置（零 IP）：设备刚加入 eNSP 尚无管理 IP 时用
export function ConsolePanel() {
  const confirm = useConfirm()
  const [port, setPort] = useState("")
  const [commands, setCommands] = useState("")
  const [saveConfig, setSaveConfig] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  const [result, setResult] = useState<DeployOutcome | null>(null)
  const [deploying, setDeploying] = useState(false)

  const cmdList = () => commands.split("\n").map((s) => s.trim()).filter(Boolean)

  const doPreview = () => {
    if (!port.trim()) { notifications.show({ color: "red", message: "请填写 Console 端口" }); return }
    const cmds = cmdList()
    if (!cmds.length) { notifications.show({ color: "red", message: "请填写配置命令" }); return }
    setPreview(cmds.join("\n"))
  }

  const doDeploy = async () => {
    if (!port.trim()) { notifications.show({ color: "red", message: "请填写 Console 端口" }); return }
    const cmds = cmdList()
    if (!cmds.length) { notifications.show({ color: "red", message: "请填写配置命令" }); return }
    const ok = await confirm("确认通过 Console 下发配置",
      "即将通过 Console 端口下发命令，该通道不会自动备份配置（无 SSH 会话）。是否确认继续？")
    if (!ok) return
    setDeploying(true)
    try {
      const data = await api("/api/console-config/raw-deploy", {
        method: "POST",
        body: JSON.stringify({ console_port: parseInt(port, 10), commands: cmds, save_config: saveConfig }),
      })
      setResult(data)
    } catch (e) {
      setResult({ success: false, error: (e as Error).message })
    } finally {
      setDeploying(false)
    }
  }

  return (
    <Paper withBorder p="md" radius="md">
      <Title order={5} mb={4}>通过 Console 端口直连配置（无需设备 IP）</Title>
      <Text size="xs" c="dimmed" mb="sm">设备刚加入 eNSP 尚无 IP 时，用此面板直接下发初始化命令</Text>

      <Stack gap="sm">
        <Group align="center" gap="xs">
          <NumberInput label="Console 端口" placeholder="例如 2000" w={160} value={port || ""}
            onChange={(v) => setPort(v == null ? "" : String(v))} />
          <Text size="xs" c="dimmed" mt={22}>eNSP 设备右键→设置查看串口号</Text>
        </Group>
        <Textarea
          label="配置命令（每行一条）" minRows={6} styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }}
          placeholder={"system-view\ninterface GigabitEthernet0/0/0\nip address 192.168.100.13 24\nquit\nstelnet server enable\n..."}
          value={commands}
          onChange={(e) => setCommands(e.currentTarget.value)}
        />
        <Group gap="md">
          <Button variant="default" leftSection={<IconEye size={14} />} onClick={doPreview}>预览</Button>
          <Button leftSection={<IconSend size={14} />} loading={deploying} onClick={doDeploy}>下发配置</Button>
          <Checkbox label="下发后保存配置" checked={saveConfig} onChange={(e) => setSaveConfig(e.currentTarget.checked)} />
        </Group>
        {preview && (
          <div>
            <Text size="sm" fw={500} mb={4}>命令预览</Text>
            <MonoPre>{preview}</MonoPre>
          </div>
        )}
        {result && <DeployResult data={result} ip={null} />}
      </Stack>
    </Paper>
  )
}
