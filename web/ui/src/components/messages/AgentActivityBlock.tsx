/**
 * Agent 时间线（M2.5）：**按发生顺序**渲染「思考 → 工具 → 结果 → 再思考」。
 *
 * 与上游分工：
 *   - 思考不再走上游（它把所有思考聚在消息顶部，见 `ours.ts` 的 reasoning 分支）；
 *   - 正文仍走上游的 text 块（Markdown 渲染复用它）；
 *   - 本组件只画"过程区"，插在最后一条助手消息里（`AssistantMessage.tsx`）。
 *
 * 视觉全部取上游设计 token，按钮用上游 `<Button>` 基元。
 */

import clsx from 'clsx'
import { useState, useSyncExternalStore } from 'react'
import { respondApproval } from '../../services/api/ours.js'
import {
  agentActivity,
  type ApprovalEntry,
  type ThinkingEntry,
  type ToolEntry,
} from '../../services/chat/agentActivity.js'
import { Button } from '../base/Button.js'
import styles from './AgentActivityBlock.module.css'

/** 把 JSON 参数压成一行预览（`{"file_path": "a.txt"}` → `a.txt`）。 */
const argsPreview = (text: string): string => {
  const raw = text.trim()
  if (!raw) return ''
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const first = Object.entries(parsed as Record<string, unknown>)[0]
      if (first) {
        const value = first[1]
        const rendered = typeof value === 'string' ? value : JSON.stringify(value)
        return rendered.length > 72 ? `${rendered.slice(0, 72)}…` : rendered
      }
    }
  } catch {
    // 不是 JSON（入参坏了/被截断）：原样显示
  }
  return raw.length > 72 ? `${raw.slice(0, 72)}…` : raw
}

const statusLabel = (status: ToolEntry['status']): string =>
  status === 'running' ? '执行中' : status === 'error' ? '失败' : '完成'

/** 思考段：默认折叠，只显示字数；展开是纯文本（不做 Markdown，思考不属于"文档"）。 */
const ThinkingBlock = ({ item }: { item: ThinkingEntry }) => {
  const [open, setOpen] = useState(false)
  return (
    <div className={styles.thinking}>
      <button type="button" className={styles.thinkingHeader} onClick={() => setOpen((v) => !v)}>
        <span className={styles.chevron}>{open ? '▾' : '▸'}</span>
        <span className={styles.thinkingTitle}>思考</span>
        <span className={styles.thinkingMeta}>{item.text.length} 字</span>
      </button>
      {open ? <pre className={styles.thinkingBody}>{item.text}</pre> : null}
    </div>
  )
}

const ToolCard = ({ item }: { item: ToolEntry }) => {
  const [open, setOpen] = useState(false)
  const preview = argsPreview(item.argumentsText)
  const hasResult = item.content.trim().length > 0
  return (
    <div className={clsx(styles.card, item.status === 'error' && styles.cardError)}>
      <button
        type="button"
        className={styles.cardHeader}
        onClick={() => setOpen((value) => !value)}
        disabled={!hasResult}
      >
        <span className={clsx(styles.dot, styles[item.status])} />
        <span className={styles.name}>{item.name}</span>
        {preview ? <span className={styles.args}>{preview}</span> : null}
        <span className={styles.status}>{statusLabel(item.status)}</span>
        {hasResult ? (
          <span className={styles.stats}>
            {item.lines} 行{item.truncated ? '（截断）' : ''}
          </span>
        ) : null}
      </button>
      {open && hasResult ? <pre className={styles.result}>{item.content}</pre> : null}
    </div>
  )
}

const ApprovalCard = ({ item }: { item: ApprovalEntry }) => {
  const [busy, setBusy] = useState(false)
  const answer = async (approved: boolean): Promise<void> => {
    setBusy(true)
    try {
      await respondApproval(item.id, approved)
      agentActivity.resolveApproval(item.id, approved)
    } finally {
      setBusy(false)
    }
  }
  const preview = argsPreview(item.argumentsText)
  return (
    <div className={clsx(styles.card, styles.approval)}>
      <div className={styles.cardHeaderStatic}>
        <span
          className={clsx(styles.dot, item.status === 'pending' ? styles.pendingDot : styles.done)}
        />
        <span className={styles.name}>{item.name}</span>
        {preview ? <span className={styles.args}>{preview}</span> : null}
        <span className={styles.status}>
          {item.status === 'pending' ? '等待批准' : item.status === 'approved' ? '已批准' : '已拒绝'}
        </span>
      </div>
      {item.status === 'pending' ? (
        <div className={styles.actions}>
          <Button variant="primary" size="s" disabled={busy} onClick={() => void answer(true)}>
            批准
          </Button>
          <Button variant="outlined" size="s" disabled={busy} onClick={() => void answer(false)}>
            拒绝
          </Button>
        </div>
      ) : null}
    </div>
  )
}

export const AgentActivityBlock = () => {
  const items = useSyncExternalStore(agentActivity.subscribe, agentActivity.snapshot)
  if (items.length === 0) return null
  return (
    <div className={styles.wrap}>
      {items.map((item) => {
        if (item.kind === 'thinking') return <ThinkingBlock key={item.id} item={item} />
        if (item.kind === 'approval') return <ApprovalCard key={`approval-${item.id}`} item={item} />
        return <ToolCard key={`tool-${item.callId}`} item={item} />
      })}
    </div>
  )
}
