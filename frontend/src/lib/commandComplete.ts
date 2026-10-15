import type { CommandCandidate } from "../types"

export interface CommandOption {
  value: string
  label: string
}

const SOURCE_TAG: Record<string, string> = {
  "?": "设备",
  base: "常用",
  catalog: "目录",
}

export function candidateLabel(c: CommandCandidate): string {
  const tag = SOURCE_TAG[c.source] || "目录"
  const desc = (c.desc || "").trim()
  return desc ? `${c.text}  ·  ${desc}  [${tag}]` : `${c.text}  [${tag}]`
}

export function toCommandOptions(candidates: CommandCandidate[]): CommandOption[] {
  const seen = new Set<string>()
  const out: CommandOption[] = []
  for (const c of candidates || []) {
    if (!c || !c.text || seen.has(c.text)) continue
    seen.add(c.text)
    out.push({ value: c.text, label: candidateLabel(c) })
  }
  return out
}
