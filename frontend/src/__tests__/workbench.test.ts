import { describe, expect, it } from "vitest"
import { extractVariables, parseVarJson, rebuildRows, rowsToJson } from "../lib/workbench"
import type { VarRow } from "../lib/workbench"

describe("extractVariables", () => {
  it("提取模板中的全部变量且去重保序", () => {
    const tpl = "interface {{interface}}\nip address {{ip}} {{mask}}\nquit\nospf {{interface}}"
    expect(extractVariables(tpl)).toEqual(["interface", "ip", "mask"])
  })

  it("无变量时返回空数组", () => {
    expect(extractVariables("sysname AR1")).toEqual([])
  })

  it("变量名限定为单词字符", () => {
    expect(extractVariables("{{a-b}} {{ok_name}} {{ok2}}")).toEqual(["ok_name", "ok2"])
  })
})

describe("rowsToJson", () => {
  it("全局值与每设备值分别归集，跳过空值", () => {
    const input: VarRow[] = [
      { name: "mask", global: "255.255.255.0", devices: {} },
      { name: "ip", global: "", devices: { "10.0.0.1": "10.0.0.1", "10.0.0.2": "" } },
      { name: "  ", global: "ignored", devices: {} },
    ]
    const { globalVars, deviceVars } = rowsToJson(input)
    expect(globalVars).toEqual({ mask: "255.255.255.0" })
    expect(deviceVars).toEqual({ "10.0.0.1": { ip: "10.0.0.1" } })
  })

  it("空行数组产出空对象", () => {
    expect(rowsToJson([])).toEqual({ globalVars: {}, deviceVars: {} })
  })
})

describe("rebuildRows", () => {
  it("模板变量按出现顺序生成行并保留旧数据", () => {
    const prev: VarRow[] = [
      { name: "ip", global: "1.1.1.1", devices: { "10.0.0.1": "1.1.1.1" } },
      { name: "old_var", global: "keep", devices: {} },
    ]
    const rows = rebuildRows("ip address {{ip}} {{mask}}", prev)
    expect(rows.map((r) => r.name)).toEqual(["ip", "mask", "old_var"])
    expect(rows[0].global).toBe("1.1.1.1")
  })

  it("新变量初始化为空行", () => {
    const rows = rebuildRows("{{fresh}}", [])
    expect(rows).toEqual([{ name: "fresh", global: "", devices: {} }])
  })
})

describe("parseVarJson", () => {
  it("从全局/设备 JSON 与模板合并变量名", () => {
    const rows = parseVarJson(
      JSON.stringify({ mask: "24" }),
      JSON.stringify({ "10.0.0.1": { ip: "10.0.0.1" } }),
      "interface {{ifname}}",
    )
    expect(rows!.map((r) => r.name).sort()).toEqual(["ifname", "ip", "mask"])
  })

  it("JSON 解析失败返回 null", () => {
    expect(parseVarJson("{bad json}", "{}", "")).toBeNull()
    expect(parseVarJson("{}", "{bad}", "")).toBeNull()
  })
})
