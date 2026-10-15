import { describe, expect, it } from "vitest"

import { candidateLabel, toCommandOptions } from "../lib/commandComplete"
import type { CommandCandidate } from "../types"

describe("candidateLabel", () => {
  it("设备实测候选标注来源", () => {
    const c: CommandCandidate = { text: "ospf", desc: "设备实测关键字", source: "?", view: "system" }
    expect(candidateLabel(c)).toContain("ospf")
    expect(candidateLabel(c)).toContain("[设备]")
  })

  it("目录候选标注来源与描述", () => {
    const c: CommandCandidate = { text: "sysname", desc: "修改设备名", source: "catalog", view: "system" }
    expect(candidateLabel(c)).toContain("sysname")
    expect(candidateLabel(c)).toContain("修改设备名")
    expect(candidateLabel(c)).toContain("[目录]")
  })

  it("基础清单候选标注来源与描述", () => {
    const c: CommandCandidate = { text: "display version", desc: "查看版本信息", source: "base", view: "user" }
    expect(candidateLabel(c)).toContain("display version")
    expect(candidateLabel(c)).toContain("查看版本信息")
    expect(candidateLabel(c)).toContain("[常用]")
  })

  it("无描述时不产生多余分隔", () => {
    const c: CommandCandidate = { text: "disp", desc: "", source: "?", view: "display" }
    expect(candidateLabel(c)).toBe("disp  [设备]")
  })
})

describe("toCommandOptions", () => {
  it("映射为 value/label 并去重", () => {
    const out = toCommandOptions([
      { text: "ospf", desc: "a", source: "?", view: "system" },
      { text: "ospf", desc: "dup", source: "catalog", view: "system" },
      { text: "vlan", desc: "b", source: "catalog", view: "system" },
    ])
    expect(out.map((o) => o.value)).toEqual(["ospf", "vlan"])
    expect(out[0].label).toContain("ospf")
  })

  it("忽略空候选", () => {
    expect(toCommandOptions([])).toEqual([])
  })
})
