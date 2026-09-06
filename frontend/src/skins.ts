import { useEffect, useState } from "react"
import { createTheme } from "@mantine/core"

// 四套外观预设：Mantine theme 覆盖 + styles.css 里 data-skin 的差异化样式
export const SKINS = [
  { id: "clean", label: "清爽简约", desc: "白卡片 · 浅灰底 · 靛蓝点缀，Linear 风", accent: "#4c6ef5" },
  { id: "glass", label: "玻璃拟态", desc: "蓝紫渐变底 · 毛玻璃卡片（旧版风格升级）", accent: "#8b5cf6" },
  { id: "tech", label: "深色科技", desc: "深蓝底 · 青色高亮 · 霓虹光晕，监控大屏风", accent: "#22d3ee" },
  { id: "vibrant", label: "渐变活力", desc: "渐变主按钮 · 彩虹顶线 · 大圆角，年轻化", accent: "#ec4899" },
]

const LS_KEY = "ensp-skin"

let listeners: Array<(id: string) => void> = []
export function getSkin(): string {
  try { return localStorage.getItem(LS_KEY) || "clean" } catch (e) { return "clean" }
}
export function setSkin(id: string): void {
  try { localStorage.setItem(LS_KEY, id) } catch (e) { /* ignore */ }
  listeners.forEach((fn) => fn(id))
}
export function subscribeSkin(fn: (id: string) => void): () => void {
  listeners.push(fn)
  return () => { listeners = listeners.filter((f) => f !== fn) }
}
export function useSkin(): string {
  const [skin, set] = useState(getSkin())
  useEffect(() => subscribeSkin(set), [])
  return skin
}

const BASE_FONT = `"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif`

export function buildTheme(skinId: string) {
  switch (skinId) {
    case "glass":
      return createTheme({
        primaryColor: "violet",
        primaryShade: 6,
        defaultRadius: "md",
        fontFamily: BASE_FONT,
        headings: { fontWeight: "700" },
      })
    case "tech":
      return createTheme({
        primaryColor: "cyan",
        primaryShade: 5,
        defaultRadius: "sm",
        fontFamily: BASE_FONT,
        headings: { fontWeight: "700" },
      })
    case "vibrant":
      return createTheme({
        primaryColor: "grape",
        primaryShade: 6,
        defaultRadius: "lg",
        fontFamily: BASE_FONT,
        headings: { fontWeight: "800" },
      })
    default: // clean
      return createTheme({
        primaryColor: "indigo",
        primaryShade: 6,
        defaultRadius: "md",
        fontFamily: BASE_FONT,
        headings: { fontWeight: "700" },
      })
  }
}
