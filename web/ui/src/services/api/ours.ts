/**
 * **我们自己的协议**（方案 2）：直接解析 `my_coder` 的 `/chat` SSE，而不是走 Anthropic 兼容层。
 *
 * 为什么要换成它：Anthropic 的协议词汇里没有"工具调用 / 审批 / 后台任务"的位置，
 * 那些是我们的核心语义。直连之后，前端拿到的是我们自己的帧：
 *
 *   queue_update → turn_start → user_message → request_start → chunk / reasoning
 *   → tool_call / tool_result / todo_update / queue_update → turn_end
 *
 * 这一版（M1）只把 **reasoning / chunk / turn_end** 映射成上游 UI 认识的
 * Anthropic 事件（thinking 块 + text 块），其余帧先"记账不用"：
 * 工具调用在 M2 会变成我们自己的卡片，审批在 M2 变成按钮。
 * 换句话说：**本文件是 M1 的过渡层，M2 起前端直接渲染我们的块**。
 */

import { agentActivity } from '../chat/agentActivity.js'
import type { MessageStreamEvent } from './types.js'

/** 服务端会话 id：一个浏览器"对话"对应一个后端 session（M3 会换成多会话列表）。 */
const SID_KEY = 'mycoder.sid'

export const oursSessionId = (): string => {
  try {
    return localStorage.getItem(SID_KEY) || 'web-ui'
  } catch {
    return 'web-ui'
  }
}

export const setOursSessionId = (sid: string): void => {
  try {
    localStorage.setItem(SID_KEY, sid)
  } catch {
    // 隐私模式下 localStorage 不可写：退化成固定会话，不影响聊天
  }
}

type OursPayload = {
  type: string
  text?: string
  turn?: number
  step?: number
  name?: string
  reason?: string
  queue?: unknown
  // 工具/审批相关的字段（M2 起渲染成卡片）
  call_id?: string
  arguments?: unknown
  content?: string
  is_error?: boolean
  chars?: number
  lines?: number
  truncated?: boolean
  id?: string
}

/** 从 Anthropic 请求体里取最后一条用户文本（`content` 可能是字符串或块数组）。 */
const lastUserText = (body: unknown): string => {
  const messages = (body as { messages?: unknown })?.messages
  if (!Array.isArray(messages)) return ''
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i] as { role?: string; content?: unknown }
    if (message?.role !== 'user') continue
    const content = message.content
    if (typeof content === 'string') return content
    if (Array.isArray(content)) {
      const texts = content
        .filter((block) => (block as { type?: string })?.type === 'text')
        .map((block) => (block as { text?: string }).text ?? '')
      if (texts.length > 0) return texts.join('')
    }
  }
  return ''
}

/** 逐帧读我们自己的 SSE（只有 `data: {...}` 行，没有 `event:` 行）。 */
async function* readFrames(
  response: Response,
  signal: AbortSignal
): AsyncGenerator<OursPayload> {
  if (!response.body) throw new Error('chat failed: empty body')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      if (signal.aborted) return
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        for (const line of block.split('\n')) {
          if (!line.startsWith('data: ')) continue
          try {
            yield JSON.parse(line.slice(6)) as OursPayload
          } catch {
            // 坏帧跳过（与旧前端一致：一帧坏了不能拖垮整条流）
          }
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

const messageStart = (): MessageStreamEvent =>
  ({
    type: 'message_start',
    message: {
      id: `msg_${Date.now().toString(16)}`,
      type: 'message',
      role: 'assistant',
      model: 'my-coder',
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: {
        input_tokens: 0,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
        output_tokens: 0,
      },
    },
  }) as unknown as MessageStreamEvent

const blockStart = (index: number, block: Record<string, string>): MessageStreamEvent =>
  ({ type: 'content_block_start', index, content_block: block }) as unknown as MessageStreamEvent

const blockDelta = (index: number, delta: Record<string, string>): MessageStreamEvent =>
  ({ type: 'content_block_delta', index, delta }) as unknown as MessageStreamEvent

const blockStop = (index: number): MessageStreamEvent =>
  ({ type: 'content_block_stop', index }) as unknown as MessageStreamEvent

const messageEnd = (stopReason: 'end_turn' | 'max_tokens' | 'tool_use' | 'refusal') =>
  ({
    type: 'message_delta',
    delta: { stop_reason: stopReason, stop_sequence: null },
    usage: {
      input_tokens: 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      output_tokens: 0,
    },
  }) as unknown as MessageStreamEvent

const STOP_EVENT = { type: 'message_stop' } as unknown as MessageStreamEvent

/**
 * 把一次 `/chat` 的流翻成上游 UI 能吃的 Anthropic 事件序列。
 *
 * 关键约定：
 * - 块**按需开**：先思考后正文；思考块一旦开了，正文开始时先 stop 再开新块；
 * - 工具调用在 M1 **不渲染**，只在回合结束时追加一行"本轮工具"提示（避免"静默干活"）；
 * - 流无论如何结束（正常 / 断线 / 取消），都必须补齐 block_stop 与 message_stop，
 *   否则上游会把这条回复永远标成"进行中"。
 */
export async function* streamOurChat(
  body: unknown,
  signal: AbortSignal
): AsyncGenerator<MessageStreamEvent> {
  const message = lastUserText(body)
  agentActivity.reset()   // 新一条回复：上一轮的工具/审批卡片全部作废
  const response = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ sid: oursSessionId(), message, request_id: crypto.randomUUID() }),
    signal,
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`chat failed: HTTP ${response.status} ${detail}`.trim())
  }

  let index = 0
  let textOpen = false
  let sawText = false
  let stopReason: 'end_turn' | 'max_tokens' | 'tool_use' | 'refusal' = 'end_turn'

  const ensureText = function* (): Generator<MessageStreamEvent> {
    if (textOpen) return
    yield blockStart(index, { type: 'text', text: '' })
    textOpen = true
  }

  yield messageStart()

  for await (const payload of readFrames(response, signal)) {
    switch (payload.type) {
      case 'reasoning': {
        // **思考不交给上游**：它会把所有思考聚在消息顶部。我们按时间线渲染
        // （思考 → 工具 → 结果 → 再思考，见 agentActivity.thinking 的合并规则）。
        agentActivity.thinking(payload.text ?? '')
        break
      }
      case 'chunk': {
        if (!payload.text) break
        yield* ensureText()
        sawText = true
        yield blockDelta(index, { type: 'text_delta', text: payload.text })
        break
      }
      case 'tool_call': {
        agentActivity.toolCall(payload as unknown as Record<string, unknown>)
        break
      }
      case 'tool_result': {
        agentActivity.toolResult(payload as unknown as Record<string, unknown>)
        break
      }
      case 'approval_request': {
        // 审批钩子直接推来的自定义载荷（不是 session 事件）：M2 起渲染成带按钮的卡片
        agentActivity.approval(payload as unknown as Record<string, unknown>)
        break
      }
      case 'turn_end': {
        if (payload.reason === 'aborted') stopReason = 'refusal'
        break
      }
      default:
        break // queue_update / todo_update / user_message / turn_start … M2 再用
    }
  }

  // 收尾：正文一次都没来时也要给一句可见的话（否则这条回复是空的）。
  // 工具调用由卡片区呈现（M2），这里不再往正文里塞工具名。
  if (!sawText) {
    yield* ensureText()
    yield blockDelta(index, { type: 'text_delta', text: '（本轮没有文本输出，工具活动见上方卡片）' })
  }

  if (textOpen) yield blockStop(index)
  yield messageEnd(stopReason)
  yield STOP_EVENT
}

/** key 校验：我们的 `/models` 永远 200（M1 不校验 key，留着 API 秒在设置台里能过）。 */
export const oursVerifyKey = async (apiKey: string, signal?: AbortSignal): Promise<void> => {
  const response = await fetch('/models', {
    headers: { Authorization: `Bearer ${apiKey}` },
    signal,
  })
  if (!response.ok) throw new Error(`verify failed: HTTP ${response.status}`)
}

/** 回应一条审批请求（`POST /approval/respond`，形状见 web/app.py 的同名路由）。 */
export const respondApproval = async (id: string, approved: boolean): Promise<void> => {
  const response = await fetch('/approval/respond', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, approved }),
  })
  if (!response.ok) throw new Error(`approval respond failed: HTTP ${response.status}`)
}
