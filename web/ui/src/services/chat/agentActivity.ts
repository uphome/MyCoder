/**
 * **Agent 时间线**（M2.5）：一条助手回复期间"思考 / 工具 / 审批"的**时序**条目。
 *
 * 为什么不用上游的 thinking/text 块：上游把所有思考聚在消息顶部，而 agent 的真实
 * 节奏是 思考 → 工具 → 结果 → 再思考。于是：
 *   - 思考**不再交给上游**（`ours.ts` 里 reasoning 帧只喂这里）；
 *   - 正文仍走上游的 text 块（Markdown 渲染复用它的能力）；
 *   - 本状态源只负责"过程区"，且**追加顺序即时间顺序**。
 *
 * 关键细节：`thinking` 只在"上一条也是 thinking"时追加合并；一旦插入工具/审批，
 * 下一段思考就会另起一条 ⇒ 时间线自然交错，不需要显式"关闭思考"。
 */

export type ThinkingEntry = { kind: 'thinking'; id: string; text: string }

export type ToolEntry = {
  kind: 'tool'
  callId: string
  name: string
  argumentsText: string
  status: 'running' | 'done' | 'error'
  content: string
  chars: number
  lines: number
  truncated: boolean
}

export type ApprovalEntry = {
  kind: 'approval'
  id: string
  name: string
  argumentsText: string
  status: 'pending' | 'approved' | 'denied'
}

export type Activity = ThinkingEntry | ToolEntry | ApprovalEntry

let items: Activity[] = []
let seq = 0
const listeners = new Set<() => void>()

const emit = (): void => {
  for (const listener of listeners) listener()
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : '')

export const agentActivity = {
  subscribe(listener: () => void): () => void {
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  },

  snapshot(): Activity[] {
    return items
  },

  /** 新一条回复开始：清空上一轮的时间线。 */
  reset(): void {
    if (items.length === 0) return
    items = []
    emit()
  },

  /** 思考增量：上一条也是思考就合并，否则另起一段（⇒ 与工具卡交错）。 */
  thinking(text: string): void {
    if (!text) return
    const last = items[items.length - 1]
    if (last && last.kind === 'thinking') {
      items = [...items.slice(0, -1), { ...last, text: last.text + text }]
    } else {
      seq += 1
      items = [...items, { kind: 'thinking', id: `thinking-${seq}`, text }]
    }
    emit()
  },

  toolCall(payload: Record<string, unknown>): void {
    const callId = asString(payload.call_id)
    if (!callId) return
    items = [
      ...items,
      {
        kind: 'tool',
        callId,
        name: asString(payload.name) || 'tool',
        argumentsText: asString(payload.arguments),
        status: 'running',
        content: '',
        chars: 0,
        lines: 0,
        truncated: false,
      },
    ]
    emit()
  },

  toolResult(payload: Record<string, unknown>): void {
    const callId = asString(payload.call_id)
    if (!callId) return
    items = items.map((item) =>
      item.kind === 'tool' && item.callId === callId
        ? {
            ...item,
            status: payload.is_error === true ? 'error' : 'done',
            content: asString(payload.content),
            chars: Number(payload.chars ?? 0) || 0,
            lines: Number(payload.lines ?? 0) || 0,
            truncated: payload.truncated === true,
          }
        : item
    )
    emit()
  },

  approval(payload: Record<string, unknown>): void {
    const id = asString(payload.id)
    if (!id) return
    if (items.some((item) => item.kind === 'approval' && item.id === id)) return
    const args = payload.arguments
    items = [
      ...items,
      {
        kind: 'approval',
        id,
        name: asString(payload.name) || 'tool',
        argumentsText: typeof args === 'string' ? args : JSON.stringify(args ?? {}),
        status: 'pending',
      },
    ]
    emit()
  },

  resolveApproval(id: string, approved: boolean): void {
    items = items.map((item) =>
      item.kind === 'approval' && item.id === id
        ? { ...item, status: approved ? 'approved' : 'denied' }
        : item
    )
    emit()
  },
}
