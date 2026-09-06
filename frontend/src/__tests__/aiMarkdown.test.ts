// @vitest-environment jsdom
import { describe, expect, it } from "vitest"
import { renderAiMarkdownHtml } from "../AiMarkdown"
import { localFallback } from "../localFallback"
import type { CapabilityResult, LastDeploy, RoleResult } from "../types"

describe("renderAiMarkdownHtml", () => {
  it("HTML 先转义，防止注入", () => {
    const html = renderAiMarkdownHtml('<script>alert(1)</script>')
    expect(html).not.toContain("<script>")
    expect(html).toContain("&lt;script&gt;")
  })

  it("围栏代码块生成可复制/发送的按钮结构", () => {
    const html = renderAiMarkdownHtml("before\n```\nsysname R1\n```\nafter")
    expect(html).toContain('class="ai-code-wrap"')
    expect(html).toContain("sysname R1")
    expect(html).toContain('data-action="copy"')
    expect(html).toContain('data-action="workbench"')
  })

  it("【模板:key】渲染为跳转按钮并携带 data-key", () => {
    const html = renderAiMarkdownHtml("试试【模板:ospf_base】的效果")
    expect(html).toContain('data-action="template"')
    expect(html).toContain('data-key="ospf_base"')
  })

  it("行内 code 与粗体", () => {
    const html = renderAiMarkdownHtml("使用 `display ip interface brief` 查看接口，**回车**发送")
    expect(html).toContain("<code>display ip interface brief</code>")
    expect(html).toContain("<strong>回车</strong>")
  })
})

describe("localFallback（无 Key 本地降级）", () => {
  const cap: CapabilityResult = {
    ip: "10.0.0.1",
    hostname: "AR1",
    model: "AR2220",
    device_role: "router",
    capabilities: {
      ospf: { supported: true, configured: true, running: true },
      bgp: { supported: false, note: "镜像未实现 BGP" },
    },
    matrix: { family: "ar_v200r003", family_label: "AR V200R003", confidence: "高" },
    interfaces: [{ name: "GE0/0/0", ip: "10.0.0.1" }],
  }

  it("能力解读：按运行中/已配置/不支持分组并标注矩阵", () => {
    const text = localFallback("explain_capabilities", "10.0.0.1", { capResult: cap, roleResult: undefined, lastDeploy: null })
    expect(text).toContain("**AR1**（10.0.0.1）")
    expect(text).toContain("镜像矩阵: AR V200R003")
    expect(text).toContain("**运行中**(1): OSPF")
    expect(text).toContain("**不支持**(1): BGP（镜像未实现 BGP）")
  })

  it("无能力数据时返回空串（调用方按 falsy 回退到模型服务）", () => {
    expect(localFallback("explain_capabilities", "10.0.0.1", {})).toBe("")
    expect(localFallback("unknown_task", "10.0.0.1", {})).toBeNull()
  })

  it("角色讲解：列出协议角色", () => {
    const roles: RoleResult = {
      ip: "10.0.0.1",
      hostname: "AR1",
      protocols: {
        vrrp: { name: "VRRP", status: "running", roles: [{ role: "Master", detail: "优先级 120", interface: "GE0/0/0" }] },
      },
    }
    const text = localFallback("explain_roles", "10.0.0.1", { roleResult: roles })
    expect(text).toContain("**AR1 的协议角色**")
    expect(text).toContain("**VRRP**: Master（优先级 120） @GE0/0/0")
  })

  it("诊断：按报错关键字给出排查清单", () => {
    const last: LastDeploy = { ip: "10.0.0.1", success: false, error: "Error: unrecognized command found", outputs: "" }
    const text = localFallback("diagnose", "10.0.0.1", { lastDeploy: last })
    expect(text).toContain("unrecognized")
    expect(text).toContain("本地诊断清单")

    const none = localFallback("diagnose", "10.0.0.1", {})
    expect(none).toContain("暂无下发失败记录")
  })

  it("诊断：超时/认证分支", () => {
    const timeout = localFallback("diagnose", "10.0.0.1", {
      lastDeploy: { ip: "", success: false, error: "connect timed out", outputs: "" },
    })
    expect(timeout).toContain("SSH 会话超时")
    const auth = localFallback("diagnose", "10.0.0.1", {
      lastDeploy: { ip: "", success: false, error: "authentication failed", outputs: "" },
    })
    expect(auth).toContain("认证失败")
  })
})
