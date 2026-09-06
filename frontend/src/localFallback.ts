// 无 Key 本地降级：确定性解读（不依赖模型服务），与旧版 _localFallback 系列对齐
import { CAPABILITY_NAMES } from "./constants"
import type { CapabilityResult, LastDeploy, RoleResult } from "./types"

export interface FallbackContext {
  capResult?: CapabilityResult
  roleResult?: RoleResult
  lastDeploy?: LastDeploy | null
}

export function localFallback(
  task: string,
  ip: string,
  { capResult, roleResult, lastDeploy }: FallbackContext,
): string | null {
  const builders: Record<string, () => string> = {
    explain_capabilities: () => localExplainCapabilities(capResult, ip),
    explain_roles: () => localExplainRoles(roleResult, ip),
    explain_config: () => localExplainConfig(capResult, ip),
    diagnose: () => localDiagnose(lastDeploy),
  }
  const fn = builders[task]
  if (!fn) return null
  return fn()
}

type CapBucket = "running" | "configured" | "supported" | "unsupported"

function localCapText(cap: CapabilityResult | undefined, ip: string): string {
  if (!cap) return ""
  const buckets: Record<CapBucket, string[]> = { running: [], configured: [], supported: [], unsupported: [] }
  for (const [key, c] of Object.entries(cap.capabilities || {})) {
    const state: CapBucket = c.running ? "running" : c.configured ? "configured" : c.supported ? "supported" : "unsupported"
    buckets[state].push(((CAPABILITY_NAMES as Record<string, string>)[key] || key) + (state === "unsupported" && c.note ? `（${c.note}）` : ""))
  }
  const m = cap.matrix || {}
  const label: Record<CapBucket, string> = { running: "运行中", configured: "已配置", supported: "支持未配置", unsupported: "不支持" }
  const lines: string[] = [`**${cap.hostname || ip}**（${ip}）${cap.model || ""} ${m.image || ""}`]
  if (m.family) lines.push(`镜像矩阵: ${m.family_label || m.family}（置信 ${m.confidence || "?"}）`)
  for (const state of ["running", "configured", "supported", "unsupported"] as CapBucket[]) {
    if (buckets[state].length) lines.push(`- **${label[state]}**(${buckets[state].length}): ${buckets[state].join("、")}`)
  }
  lines.push("\n状态含义: 运行中 = 已配置且检测到邻居/表项；已配置 = 配置里出现关键字；支持 = 命令探测通过。")
  return lines.join("\n")
}

function localExplainCapabilities(cap: CapabilityResult | undefined, ip: string): string {
  const text = localCapText(cap, ip)
  if (!text) return ""
  return text
    + "\n\n为什么有不支持的？eNSP 镜像功能是真实 VRP 的子集（S5700 是 V200R001、AR 是 V200R003），矩阵判定与 ? 命令树实测一致时最可信。"
}

function localExplainRoles(roleResult: RoleResult | undefined, ip: string): string {
  const roles = roleResult?.protocols || {}
  const lines: string[] = [`**${roleResult?.hostname || ip} 的协议角色**`]
  let any = false
  for (const p of Object.values(roles)) {
    if (!p.roles?.length) continue
    any = true
    const chips = p.roles.map((r) => `${r.role}` + (r.detail ? `（${r.detail}）` : "") + (r.interface ? ` @${r.interface}` : ""))
    lines.push(`- **${p.name}**: ${chips.join("；")}`)
  }
  if (!any) lines.push("- 暂未发现已配置的协议角色，请先完成配置或重新识别能力。")
  return lines.join("\n")
}

function localExplainConfig(cap: CapabilityResult | undefined, ip: string): string {
  if (!cap) return ""
  const intfs = cap.interfaces || []
  const withIp = intfs.filter((i) => i.ip).length
  const lines: string[] = [`**${cap.hostname || ip} 配置摘要**`]
  lines.push(`- 接口 ${intfs.length} 个（配 IP ${withIp} 个）`)
  if (localCapText(cap, ip)) lines.push("- 协议状态: 见\"能力解读\"的分组清单")
  lines.push("- 配置全文可在\"配置下发\"弹窗中查看当前配置；逐段讲解需接入 AI 服务。")
  return lines.join("\n")
}

function localDiagnose(last?: LastDeploy | null): string {
  const err = (last?.error || "").toLowerCase()
  const lines: string[] = ["**本地诊断清单**" + (last?.error ? `（最近报错: ${last.error}）` : "")]
  if (err.includes("unrecognized")) {
    lines.push("1. 报错含义: 该镜像不认识此命令——大概率设备确实不支持该功能。")
    lines.push("2. 先看\"能力识别\"里这项协议是否标记不支持；再看矩阵徽标的镜像家族。")
    lines.push("3. 改用\"协议角色\"页对应协议的填空模板，模板只发设备支持的命令。")
  } else if (err.includes("timeout") || err.includes("timed out")) {
    lines.push("1. SSH 会话超时: 检查管理网段 IP/掩码、Windows 虚拟网卡、代理/TUN 是否劫持。")
    lines.push("2. 设备侧验证: 用下方\"连通性验证\"ping 管理地址。")
  } else if (err.includes("authentication") || err.includes("auth")) {
    lines.push("1. 认证失败: 核对 local-user 用户名/密码与设备列表里配置一致。")
    lines.push("2. 检查 aaa 与 user-interface vty 认证模式（aaa / password）。")
  } else if (err) {
    lines.push("1. 核对命令回显中 ^ 指示的出错位置（VRP 会标注错误列）。")
    lines.push("2. 分段执行命令，先 system-view 再进视图，确认视图层级正确。")
  } else {
    lines.push("1. 暂无下发失败记录。遇到问题把报错原文发给助教即可。")
  }
  lines.push("3. 每次修改前系统都会自动备份，backups/ 目录可回退。")
  return lines.join("\n")
}
