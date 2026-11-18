/**
 * 工具卡 + 审批卡（M2）。
 *
 * 视觉全部取自上游：容器用 `AgentActivityBlock.module.css` 里的设计 token，
 * 按钮直接用上游的 `<Button>` 基元（variant/size 同款），所以和设置台、输入区
 * 那些按钮是同一套尺寸与配色。
 *
 * 一张卡 = 一次工具调用；状态点表示 running/done/error；点抬头展开结果。
 * 审批卡带**批准 / 拒绝**两个按钮，直接打 `POST /approval/respond`。
 */

import clsx from 'clsx'
import { useState, useSyncExternalStore } from 'react'
import { respondApproval } from '../../services/api/ours.js'
import {
  agentActivity,
  type ApprovalActivity,
  type ToolActivity,
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
      const entries = Object.entries(parsed as Record<string, unknown>)
      const first = entries[0]
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

const statusLabel = (status: ToolActivity['status']): string =>
  status === 'running' ? '执行中' : status === 'error' ? '失败' : '完成'

const ToolCard = ({ item }: { item: ToolActivity }) => {
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

const ApprovalCard = ({ item }: { item: ApprovalActivity }) => {
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
        <span className={clsx(styles.dot, item.status === 'pending' ? styles.pendingDot : styles.done)} />
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
      {items.map((item) =>
        item.kind === 'approval' ? (
          <ApprovalCard key={`approval-${item.id}`} item={item} />
        ) : (
          <ToolCard key={`tool-${item.callId}`} item={item} />
        )
      )}
    </div>
  )
}
