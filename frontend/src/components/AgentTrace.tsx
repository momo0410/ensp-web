import { Badge, Group, Text } from "@mantine/core"
import type { AgentTraceItem } from "../types"

/**
 * 渲染 Agent 的执行轨迹：每一步调了什么工具、带什么参数、拿到什么结果。
 *
 * 存在的意义是**可审计**——用户能看见 Agent 到底查了什么才得出结论，
 * 而不是只拿到一段无法验证的答案。工具失败也如实展示，不隐藏。
 */

/** 工具名 → 中文短标签，让轨迹一眼可读。 */
const TOOL_LABEL: Record<string, string> = {
  list_devices: "查看设备表",
  get_device_detail: "读取设备实测",
  get_current_config: "读取当前配置",
  get_ensp_status: "查询 eNSP 状态",
  list_topologies: "列出拓扑",
  search_commands: "检索命令目录",
  ping: "设备侧 ping",
  trace_path: "追踪转发路径",
  complete_command: "命令补全",
  collect_device: "采集设备信息",
  probe_capabilities: "识别协议能力",
  propose_config: "生成配置方案（待确认）",
  propose_topology: "生成拓扑方案（待确认）",
}

function labelOf(tool?: string): string {
  if (!tool) return "工具"
  return TOOL_LABEL[tool] || tool
}

/** 把参数压成一行摘要，避免长 JSON 撑爆面板。 */
function summarizeArgs(args?: Record<string, unknown>): string {
  if (!args || !Object.keys(args).length) return ""
  return Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join("  ")
    .slice(0, 160)
}

/** 从工具结果里挑一句最有信息量的话做摘要。 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function summarizeResult(result: any): string {
  if (!result || typeof result !== "object") return String(result ?? "")
  if (result.error) return String(result.error)
  if (typeof result.count === "number") return `命中 ${result.count} 条`
  if (Array.isArray(result.devices)) return `${result.devices.length} 台设备`
  if (Array.isArray(result.matches)) return `${result.matches.length} 条匹配`
  if (Array.isArray(result.candidates)) return `${result.candidates.length} 个候选`
  if (result.summary && typeof result.summary === "string") return result.summary
  if (Array.isArray(result.hops)) return `${result.hops.length} 跳`
  if (result.received != null) {
    return `收包 ${result.received}/${result.transmitted}`
  }
  if (result.config !== undefined) {
    return result.config ? `${String(result.config).length} 字符配置` : "无配置数据"
  }
  if (result.targets) return `目标 ${(result.targets || []).length} 台`
  if (result.note) return String(result.note)
  return ""
}

export function AgentTrace({ items }: { items: AgentTraceItem[] }) {
  if (!items.length) return null
  return (
    <div className="agent-trace">
      <Text size="xs" c="dimmed" mb={4}>执行轨迹（{items.length} 步）</Text>
      {items.map((it, i) => {
        if (it.kind === "tool_call") {
          const args = summarizeArgs(it.args)
          return (
            <Group key={i} gap={6} wrap="nowrap" align="baseline">
              <Badge size="xs" variant="light" color="blue">步骤 {it.step}</Badge>
              <Text size="xs">{labelOf(it.tool)}</Text>
              {args && <Text size="xs" c="dimmed" truncate>（{args}）</Text>}
            </Group>
          )
        }
        if (it.kind === "tool_result") {
          const summary = summarizeResult(it.result)
          return (
            <Group key={i} gap={6} wrap="nowrap" align="baseline" pl="md">
              <Badge size="xs" variant="light" color={it.ok ? "teal" : "red"}>
                {it.ok ? "成功" : "失败"}
              </Badge>
              {summary && (
                <Text size="xs" c={it.ok ? "dimmed" : "red"} truncate>{summary}</Text>
              )}
            </Group>
          )
        }
        if (it.kind === "error") {
          return (
            <Group key={i} gap={6} wrap="nowrap" align="baseline">
              <Badge size="xs" variant="light" color="orange">中断</Badge>
              <Text size="xs" c="orange">{it.text}</Text>
            </Group>
          )
        }
        return (
          <Group key={i} gap={6} wrap="nowrap" align="baseline">
            <Badge size="xs" variant="light" color="gray">提示</Badge>
            <Text size="xs" c="dimmed">{it.text}</Text>
          </Group>
        )
      })}
    </div>
  )
}
