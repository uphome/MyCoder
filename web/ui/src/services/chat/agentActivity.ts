/**
 * **Agent 活动**（M2）：一条助手回复期间"工具在干什么 / 谁在等审批"的实时状态。
 *
 * 为什么不用上游的消息 part 模型：它只有 thinking / text 两种块（那是"聊天"的语义），
 * 而 agent 的真身是**工具调用序列 + 审批**。我们没有改它的消息类型（那是把上游的
 * reducer/渲染/分支全都要动），而是在旁边挂一个**独立的小状态源**：
 *
 *   上游 UI 负责"说了什么"（text/thinking），我们负责"做了什么"（tool/approval）。
 *
 * 数据只从 `ours.ts` 的流里来（`tool_call` / `tool_result` / `approval_request`），
 * 一条回复开始时 `reset()`；组件用 `useSyncExternalStore` 订阅，不引入新状态库。
 */

export type ToolActivity = {
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

export type ApprovalActivity = {
  kind: 'approval'
  id: string
  name: string
  argumentsText: string
  status: 'pending' | 'approved' | 'denied'
}

export type Activity = ToolActivity | ApprovalActivity

let items: Activity[] = []
const listeners = new Set<() => void>()

const emit = (): void => {
  for (const listener of listeners) listener()
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : '')

export const agentActivity = {
  snapshot: (): Activity[] => items,

  subscribe(listener: () => void): () => void {
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  },

  /** 新一条回复开始：清空上一轮的活动（上游已经把消息流切到新消息了）。 */
  reset(): void {
    if (items.length === 0) return
    items = []
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
