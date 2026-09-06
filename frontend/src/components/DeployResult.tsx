import React, { useEffect } from "react"
import { Button, Group, Stack, Text } from "@mantine/core"
import { useApp } from "../store"
import { MonoPre } from "../ui"
import type { CommandOutput, DeployOutcome } from "../types"

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <Text size="xs" c="dimmed" mb={4}>{label}</Text>
      {children}
    </div>
  )
}

function CmdOutput({ outputs }: { outputs: CommandOutput[] }) {
  const text = outputs.map((o) => `${o.success ? "✓" : "✗"} ${o.command}\n${o.output || o.error || ""}`).join("\n\n")
  return <MonoPre>{text}</MonoPre>
}

/**
 * 统一下发结果渲染器（对应旧版 renderDeployResult）。
 * 渲染即记录 lastDeploy，供 AI 助教“诊断报错”使用。
 */
export function DeployResult({ data, ip }: { data: DeployOutcome | null; ip: string | null }) {
  const { setLastDeploy } = useApp()

  useEffect(() => {
    if (!data) return
    setLastDeploy({
      ip: ip || "",
      success: !!data.success,
      error: data.success ? "" : (data.error || "下发失败（无详细错误信息）"),
      outputs: (data.command_outputs || [])
        .map((o) => `${o.success ? "✓" : "✗"} ${o.command}\n${o.output || o.error || ""}`)
        .join("\n\n")
        .slice(0, 2000),
    })
  }, [data, ip, setLastDeploy])

  if (!data) return null

  return (
    <Stack gap="sm" mt="sm">
      {data.backup_file && <Row label="备份文件"><Text size="sm">{data.backup_file}</Text></Row>}
      {(data.commands?.length || 0) > 0 && <Row label="下发命令"><MonoPre>{(data.commands || []).join("\n")}</MonoPre></Row>}
      {(data.command_outputs?.length || 0) > 0 && <Row label="执行回显"><CmdOutput outputs={data.command_outputs || []} /></Row>}
      {(data.verify_outputs?.length || 0) > 0 && <Row label="验证结果"><CmdOutput outputs={data.verify_outputs || []} /></Row>}
      {data.save_result && <Row label="保存配置"><Text size="sm">{data.save_result}</Text></Row>}
      <Row label="状态">
        <Text size="sm" fw={700} c={data.success ? "teal" : "red"}>
          {data.success ? "下发成功" : `下发失败: ${data.error || "未知错误"}`}
        </Text>
      </Row>
      {!data.success && <DiagnoseButton />}
      {data.warning && <Text size="sm" c="yellow">{data.warning}</Text>}
    </Stack>
  )
}

function DiagnoseButton() {
  const { aiBus } = useApp()
  return (
    <Group>
      <Button size="xs" onClick={() => aiBus.current?.openDiagnose?.()}>
        🤖 AI 诊断这次失败
      </Button>
    </Group>
  )
}
