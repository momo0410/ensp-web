import React, { createContext, useContext, useState } from "react"
import { Alert, Badge, Button, Group, Loader, Modal, Text } from "@mantine/core"
import { IconInfoCircle, IconCheck, IconX } from "@tabler/icons-react"
import { useApp } from "./store"
import type { CapabilityItem } from "./types"

// ---------- 批量状态条（对应旧版 batch-status） ----------
const STATUS_COLORS = { info: "blue", success: "teal", error: "red", warning: "yellow", loading: "blue" }

export function BatchStatusBanner() {
  const { batchStatus, flashStatus } = useApp()
  if (!batchStatus) return null
  const color = STATUS_COLORS[batchStatus.type] || "blue"
  const icon = batchStatus.type === "loading"
    ? <Loader size={16} color={color} />
    : batchStatus.type === "success" ? <IconCheck size={16} />
      : batchStatus.type === "error" ? <IconX size={16} />
        : <IconInfoCircle size={16} />
  return (
    <Alert color={color} variant="light" icon={icon} withCloseButton onClose={() => flashStatus(null)}>
      {batchStatus.message}
    </Alert>
  )
}

// ---------- 三态徽标 / 证据徽标 ----------
export function CapBadge({ kind, children, title }: {
  kind: string
  children: React.ReactNode
  title?: string
}) {
  const colors: Record<string, string> = { supported: "blue", configured: "teal", running: "grape", unsupported: "red", evidence: "gray", matrix: "indigo" }
  return <Badge color={colors[kind] || "gray"} variant="light" size="sm" title={title}>{children}</Badge>
}

export function EvidenceBadge({ cap }: { cap?: CapabilityItem | null }) {
  if (!cap || !cap.evidence) return null
  const ev = String(cap.evidence)
  let tag = ""
  if (ev.includes("helptree")) tag = "?树实测"
  else if (ev.includes("matrix")) tag = "镜像矩阵"
  if (!tag) return null
  return <CapBadge kind="evidence" title={`判定依据: ${ev}${cap.note ? " | " + cap.note : ""}`}>{tag}</CapBadge>
}

// ---------- 确认弹窗（Promise 化 showConfirm） ----------
const ConfirmCtx = createContext<(title: string, message: string) => Promise<boolean>>(() => Promise.resolve(false))
export const useConfirm = () => useContext(ConfirmCtx)

export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<{ title: string; message: string; resolve: (ok: boolean) => void } | null>(null)
  const showConfirm = (title: string, message: string): Promise<boolean> =>
    new Promise((resolve) => setState({ title, message, resolve }))
  const close = (ok: boolean) => { if (state) state.resolve(ok); setState(null) }
  return (
    <ConfirmCtx.Provider value={showConfirm}>
      {children}
      <Modal opened={!!state} onClose={() => close(false)} title={state?.title} centered size="sm">
        <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>{state?.message}</Text>
        <Group justify="flex-end" mt="lg">
          <Button variant="default" onClick={() => close(false)}>取消</Button>
          <Button onClick={() => close(true)}>确定</Button>
        </Group>
      </Modal>
    </ConfirmCtx.Provider>
  )
}

// ---------- 通用小件 ----------
// 后端模板/目录的 icon 字段存的是 HTML 实体（如 &#x1F4E4;），文本节点里需先解码
export function decodeEntities(s: string | undefined | null): string {
  if (!s || !s.includes("&#") && !s.includes("&")) return s || ""
  const t = document.createElement("textarea")
  t.innerHTML = s
  return t.value
}

export function MonoPre({ children, style }: { children?: React.ReactNode; style?: React.CSSProperties }) {
  return <pre className="mono-pre" style={style}>{children ?? ""}</pre>
}

export function EmptyHint({ children }: { children?: React.ReactNode }) {
  return <Text c="dimmed" size="sm" ta="center" py="lg">{children}</Text>
}

export function OnlineBadge({ ok, okText = "在线", failText = "离线", unknownText = "未知" }: {
  ok?: boolean | null
  okText?: string
  failText?: string
  unknownText?: string
}) {
  if (ok === undefined || ok === null) return <Badge color="gray" variant="light" size="sm">{unknownText}</Badge>
  return ok
    ? <Badge color="teal" variant="light" size="sm">{okText}</Badge>
    : <Badge color="red" variant="light" size="sm">{failText}</Badge>
}
