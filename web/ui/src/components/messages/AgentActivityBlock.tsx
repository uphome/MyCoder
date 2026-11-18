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
import { IconChevronDownOutline14, IconChevronRightOutline14, IconThinkOutline16 } from '../base/icons.js'
import { Button } from '../base/Button.js'
import { Markdown } from '../markdown/Markdown.js'
import './thinking.markdown.css'
import { ContentItem } from './ReplyArea.js'
import replyStyles from './ReplyArea.module.css'
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

/**
 * 思考段：**完全复用上游的思考样式**——
 * 标题行用 `ReplyArea.module.css` 的 title/titleIcon/titleText（含 `IconThinkOutline16`），
 * 正文用上游的 `ContentItem`（它挂 `ds-think-content`，`thinking.markdown.css` 就作用在它上面）
 * + `Markdown`。唯一的变化是**位置**：它现在跟着工具卡按时间顺序排，而不是全堆在顶部。
 */
const ThinkingBlock = ({ item, streaming }: { item: ThinkingEntry; streaming: boolean }) => {
  const [open, setOpen] = useState(true)
  return (
    <div>
      <div className={replyStyles.title} onClick={() => setOpen((value) => !value)}>
        <span className={replyStyles.titleIcon}>
          <IconThinkOutline16 />
        </span>
        <span className={replyStyles.titleText}>思考</span>
        {open ? <IconChevronDownOutline14 /> : <IconChevronRightOutline14 />}
      </div>
      {open ? (
        <div className={replyStyles.steps}>
          <ContentItem active={streaming} isLast>
            <Markdown
              content={item.text}
              loading={false}
              cacheKey={`thinking-${item.id}`}
              disableLink
            />
          </ContentItem>
        </div>
      ) : null}
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

export const AgentActivityBlock = ({ streaming = false }: { streaming?: boolean } = {}) => {
  const items = useSyncExternalStore(agentActivity.subscribe, agentActivity.snapshot)
  if (items.length === 0) return null
  return (
    <div className={styles.wrap}>
      {items.map((item) => {
        if (item.kind === 'thinking')
          return <ThinkingBlock key={item.id} item={item} streaming={streaming} />
        if (item.kind === 'approval') return <ApprovalCard key={`approval-${item.id}`} item={item} />
        return <ToolCard key={`tool-${item.callId}`} item={item} />
      })}
    </div>
  )
}
