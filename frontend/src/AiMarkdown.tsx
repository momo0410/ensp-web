import React from "react"

function escapeHtml(text: string): string {
  return String(text)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;")
}

/**
 * AI 消息渲染：``` 围栏代码块（复制 / 发送到工作台）、
 * 【模板:key】跳转按钮、行内 code、**粗体**。与旧版 _aiRenderMarkdown 对齐。
 * 交互通过事件委托（data-action）处理。
 */
export function renderAiMarkdownHtml(text: string): string {
  const escaped = escapeHtml(text)
  const parts = escaped.split(/```(?:\w*\n)?([\s\S]*?)```/g)
  let html = ""
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      const code = parts[i].replace(/\n$/, "")
      html += `<div class="ai-code-wrap"><pre class="ai-code">${code}</pre>
        <div class="ai-code-actions">
          <button type="button" class="mantine-Button-root" data-action="copy" style="font-size:12px;padding:2px 10px;border:1px solid var(--mantine-color-default-border);border-radius:6px;background:var(--mantine-color-default);cursor:pointer">复制</button>
          <button type="button" data-action="workbench" style="font-size:12px;padding:2px 10px;border:1px solid var(--mantine-color-default-border);border-radius:6px;background:var(--mantine-color-default);cursor:pointer">→ 发送到工作台</button>
        </div></div>`
    } else {
      html += parts[i]
        .replace(/【模板:([a-zA-Z0-9_]+)】/g, (m, key: string) =>
          `<button type="button" data-action="template" data-key="${key}" style="font-size:12px;padding:2px 10px;border-radius:6px;border:1px solid transparent;background:var(--mantine-color-blue-filled-color);color:#fff;cursor:pointer">☞ 打开填空模板: ${key}</button>`)
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\n/g, "<br>")
    }
  }
  return html
}

interface AiMarkdownProps {
  text: string
  onTemplate?: (key: string) => void
}

export function AiMarkdown({ text, onTemplate }: AiMarkdownProps) {
  const handle = (e: React.MouseEvent<HTMLDivElement>) => {
    const btn = (e.target as HTMLElement).closest("[data-action]") as HTMLElement | null
    if (!btn) return
    const action = btn.dataset.action
    if (action === "copy") {
      const pre = btn.closest(".ai-code-wrap")?.querySelector("pre")
      if (pre) {
        navigator.clipboard?.writeText(pre.textContent || "").then(() => {
          btn.textContent = "已复制"
          setTimeout(() => { btn.textContent = "复制" }, 1500)
        })
      }
    } else if (action === "workbench") {
      const pre = btn.closest(".ai-code-wrap")?.querySelector("pre")
      if (pre) window.dispatchEvent(new CustomEvent("ensp:ai-to-workbench", { detail: pre.textContent?.trim() }))
    } else if (action === "template") {
      onTemplate?.(btn.dataset.key || "")
    }
  }
  return <div onClick={handle} dangerouslySetInnerHTML={{ __html: renderAiMarkdownHtml(text) }} />
}
