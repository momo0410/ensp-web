// 自由配置工作台的纯函数（独立于 React，便于单测）

export interface VarRow {
  name: string
  global: string
  devices: Record<string, string>
}

/** 从命令模板提取 {{变量名}}（\w+ 单词变量） */
export function extractVariables(template: string): string[] {
  const vars = new Set<string>()
  const pattern = /\{\{(\w+)\}\}/g
  let match: RegExpExecArray | null
  while ((match = pattern.exec(template)) !== null) vars.add(match[1])
  return Array.from(vars)
}

/** 变量行 → { globalVars, deviceVars }，跳过空值（等价旧版 syncVarTableToJson） */
export function rowsToJson(rows: VarRow[]): { globalVars: Record<string, string>; deviceVars: Record<string, Record<string, string>> } {
  const globalVars: Record<string, string> = {}
  const deviceVars: Record<string, Record<string, string>> = {}
  for (const row of rows) {
    const name = row.name.trim()
    if (!name) continue
    if (row.global.trim()) globalVars[name] = row.global.trim()
    for (const [ip, val] of Object.entries(row.devices)) {
      if (!val.trim()) continue
      if (!deviceVars[ip]) deviceVars[ip] = {}
      deviceVars[ip][name] = val.trim()
    }
  }
  return { globalVars, deviceVars }
}

/** 模板变量重建行序：模板变量在前（保留旧数据），手动行跟在后面 */
export function rebuildRows(template: string, prev: VarRow[]): VarRow[] {
  const vars = extractVariables(template)
  const byName: Record<string, VarRow> = {}
  for (const row of prev) byName[row.name] = row
  const next: VarRow[] = vars.map((name) => byName[name] || { name, global: "", devices: {} })
  for (const row of prev) {
    if (row.name.trim() && !vars.includes(row.name)) next.push(row)
  }
  return next
}

/** 从 JSON 文本解析变量（等价旧版 _populateTableFromJson 的解析部分），返回 null 表示解析失败 */
export function parseVarJson(jsonGlobal: string, jsonDevice: string, template: string): VarRow[] | null {
  let globalVars: Record<string, string> = {}
  let deviceVars: Record<string, Record<string, string>> = {}
  try {
    if (jsonGlobal.trim() && jsonGlobal.trim() !== "{}") globalVars = JSON.parse(jsonGlobal)
  } catch (e) {
    return null
  }
  try {
    if (jsonDevice.trim() && jsonDevice.trim() !== "{}") deviceVars = JSON.parse(jsonDevice)
  } catch (e) {
    return null
  }
  const allNames = new Set<string>([
    ...Object.keys(globalVars),
    ...Object.values(deviceVars).flatMap((d) => Object.keys(d)),
    ...extractVariables(template),
  ])
  return Array.from(allNames).map((name) => {
    const devicesMap: Record<string, string> = {}
    for (const [ip, dv] of Object.entries(deviceVars)) {
      if (dv[name]) devicesMap[ip] = dv[name]
    }
    return { name, global: globalVars[name] || "", devices: devicesMap }
  })
}
