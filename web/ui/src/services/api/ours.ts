/**
 * **我们自己的协议**（方案 2）：直接解析 `my_coder` 的 `/chat` SSE，而不是走 Anthropic 兼容层。
 *
 * 为什么：Anthropic 的协议词汇里没有"工具调用 / 审批 / 后台任务"的位置，那些是我们的
 * 核心语义。直连之后，前端拿到的是我们自己的帧：
 *
 *   queue_update → turn_start → user_message → request_start → chunk / reasoning
 *   → tool_call / tool_result / todo_update / queue_update → turn_end
 *
 * M1 只映射 **reasoning / chunk / turn_end**（上游 UI 认识的 thinking 块 + text 块），
 * 其余帧先"记账不用"。**本文件是过渡层**：等我们的块类型进前端后，前端直接渲染。
 */

import type { MessageStreamEvent } from './types.js'

/** 服务端会话 id：**每个浏览器一个**（首次访问生成），避免多标签页共用一份历史。 */
const SID_KEY = 'mycoder.sid'
/** localStorage 不可用（隐私模式等）时的兜底会话——此时多标签确实会共享，已在文档说明。 */
const FALLBACK_SID = 'web-ui'

/** 生成随机 id：`crypto.randomUUID` 只在安全上下文可用（局域网 http 访问会缺失）。 */
const randomId = (): string => {
  const cryptoApi = globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    cryptoApi.getRandomValues(bytes)
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  }
  return Math.random().toString(16).slice(2)
}

export const oursSessionId = (): string => {
  try {
    const existing = localStorage.getItem(SID_KEY)
    if (existing) return existing
    const fresh = `web-${randomId()}`
    localStorage.setItem(SID_KEY, fresh)
    return fresh
  } catch {
    return FALLBACK_SID
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
        .map((block) => (block as { text?: unknown }).text)
        .filter((text): text is string => typeof text === 'string')
      if (texts.length > 0) return texts.join('')
    }
  }
  return ''
}

/** 解析一段 SSE 帧块（容忍 `data:` 后有没有空格；坏帧跳过）。 */
function* parseFrames(block: string): Generator<OursPayload> {
  for (const rawLine of block.split('\n')) {
    const line = rawLine.trimEnd()
    if (!line.startsWith('data:')) continue
    const payloadText = line.slice('data:'.length).trimStart()
    if (!payloadText) continue
    try {
      yield JSON.parse(payloadText) as OursPayload
    } catch {
      // 坏帧跳过：一帧坏了不能拖垮整条流
    }
  }
}

/** 逐帧读我们自己的 SSE（只有 `data: {...}` 行，没有 `event:` 行）。 */
async function* readFrames(response: Response, signal: AbortSignal): AsyncGenerator<OursPayload> {
  if (!response.body) throw new Error('chat failed: empty body')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      if (signal.aborted) {
        // 用户点了停止：**主动取消读取**，让连接立刻释放（否则要等服务端 EOF，
        // 后端 sse_stream 的 finally 也就不能及时取消 agent）。
        await reader.cancel()
        return
      }
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        yield* parseFrames(block)
        boundary = buffer.indexOf('\n\n')
      }
    }
    // 连接结束时最后一段可能没有以空行收尾：flush 一次，否则最后一帧（常是 turn_end）会丢
    if (buffer.trim()) yield* parseFrames(buffer)
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
 * 约定：
 * - 思考块先于正文开（正文开始时先 stop 思考块）；
 * - 工具调用在 M1 **不渲染**，只在回合结束时追加一行"本轮工具"提示（避免"静默干活"）；
 * - **异常/中止也要补齐 block_stop 与 message_stop**：否则上游会把这条回复永远标成"进行中"。
 */
export async function* streamOurChat(
  body: unknown,
  signal: AbortSignal
): AsyncGenerator<MessageStreamEvent> {
  const message = lastUserText(body)
  if (!message.trim()) {
    // 纯附件消息在 M1 不支持（附件接口还没接）：必须让用户看见原因，别发一个必然 400 的请求
    throw new Error('这条消息没有可发送的文本；M1 暂不支持只发附件的消息')
  }
  const response = await fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ sid: oursSessionId(), message, request_id: randomId() }),
    signal,
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`chat failed: HTTP ${response.status} ${detail}`.trim())
  }

  let index = 0
  let thinkingOpen = false
  let textOpen = false
  let sawText = false
  let stopReason: 'end_turn' | 'max_tokens' | 'tool_use' | 'refusal' = 'end_turn'
  const tools: string[] = []

  const ensureText = function* (): Generator<MessageStreamEvent> {
    if (textOpen) return
    if (thinkingOpen) {
      yield blockStop(index)
      thinkingOpen = false
      index += 1
    }
    yield blockStart(index, { type: 'text', text: '' })
    textOpen = true
  }

  /** 收尾：关掉还开着的块并补 message_stop（正常/异常/中止都要走到）。 */
  const closeOpenBlocks = function* (): Generator<MessageStreamEvent> {
    if (thinkingOpen) {
      yield blockStop(index)
      thinkingOpen = false
      index += 1
    }
    if (textOpen) yield blockStop(index)
    yield messageEnd(stopReason)
    yield STOP_EVENT
  }

  yield messageStart()

  try {
    for await (const payload of readFrames(response, signal)) {
      switch (payload.type) {
        case 'reasoning': {
          if (!payload.text) break
          if (textOpen) break // 正文已经开始，迟到的思考不追（上游也没有"回退"语义）
          if (!thinkingOpen) {
            yield blockStart(index, { type: 'thinking', thinking: '', signature: '' })
            thinkingOpen = true
          }
          yield blockDelta(index, { type: 'thinking_delta', thinking: payload.text })
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
          if (payload.name) tools.push(payload.name)
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
  } catch (error) {
    // 网络中断 / fetch 被 abort：先把该关的关上，别让 UI 停在"进行中"
    yield* closeOpenBlocks()
    throw error
  }

  // 收尾：正文一次都没来时也要给一句可见的话（否则这条回复是空的）
  if (!sawText) {
    yield* ensureText()
    const note = tools.length > 0 ? `（本轮只执行了工具：${tools.join('、')}）` : '（本轮没有文本输出）'
    yield blockDelta(index, { type: 'text_delta', text: note })
  } else if (tools.length > 0) {
    yield blockDelta(index, { type: 'text_delta', text: `\n\n> 🔧 本轮工具：${tools.join('、')}` })
  }
  yield* closeOpenBlocks()
}

/** key 校验：我们的 `/models` 永远 200（M1 不校验 key，留着设置台能过）。 */
export const oursVerifyKey = async (apiKey: string, signal?: AbortSignal): Promise<void> => {
  const response = await fetch('/models', {
    headers: { Authorization: `Bearer ${apiKey}` },
    signal,
  })
  if (!response.ok) throw new Error(`verify failed: HTTP ${response.status}`)
}
