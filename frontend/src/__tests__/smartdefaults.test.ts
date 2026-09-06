import { describe, expect, it } from "vitest"

// 智能默认值填充逻辑来自 store/WorkbenchView 内联实现，
// 这里以同规则验证其纯数据行为：只填空值，不覆盖已填内容。
import { networkToWildcardJS } from "../netutil"

interface VarRow {
  name: string
  global: string
  devices: Record<string, string>
}

function applySmartDefaults(rows: VarRow[], defaults: Record<string, { router_id?: string }>): VarRow[] {
  return rows.map((row) => {
    const name = row.name.trim()
    if (name !== "router_id" && name !== "router-id") return row
    const nextDevices = { ...row.devices }
    for (const [ip, d] of Object.entries(defaults)) {
      if (d.router_id && !(nextDevices[ip] || "").trim()) nextDevices[ip] = d.router_id
    }
    let global = row.global
    if (!global.trim()) {
      const firstIp = Object.keys(defaults)[0]
      if (firstIp && defaults[firstIp].router_id) global = defaults[firstIp].router_id
    }
    return { ...row, global, devices: nextDevices }
  })
}

describe("applySmartDefaults（router_id 填充规则）", () => {
  it("只填充空单元格，不覆盖已填值", () => {
    const rows: VarRow[] = [
      { name: "router_id", global: "", devices: { "10.0.0.1": "1.1.1.1", "10.0.0.2": "" } },
    ]
    const out = applySmartDefaults(rows, {
      "10.0.0.1": { router_id: "9.9.9.9" },
      "10.0.0.2": { router_id: "2.2.2.2" },
    })
    expect(out[0].devices["10.0.0.1"]).toBe("1.1.1.1")
    expect(out[0].devices["10.0.0.2"]).toBe("2.2.2.2")
    expect(out[0].global).toBe("9.9.9.9") // 全局为空时取第一台设备
  })

  it("非 router_id 变量不受影响", () => {
    const rows: VarRow[] = [{ name: "mask", global: "", devices: { "10.0.0.1": "" } }]
    const out = applySmartDefaults(rows, { "10.0.0.1": { router_id: "1.1.1.1" } })
    expect(out[0]).toEqual(rows[0])
  })
})

describe("network → wildcard 联动（工作台同规则）", () => {
  it("network 行全局值可推导 wildcard 全局值", () => {
    const network = "192.168.12.0"
    const wc = networkToWildcardJS(network)
    expect(wc).toBe("0.0.0.255")
  })
})
