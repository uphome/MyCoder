"""Anthropic 兼容层（M0：**mock**，只为了让新前端跑起来能看见效果）。

为什么需要这一层：`web/ui/` 是从 [deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo)
fork 来的视觉层，它的 API 客户端说的是 **Anthropic Messages API**（`POST /anthropic/v1/messages`，
SSE）。我们**不打算**长期维护这份翻译——M1 会把前端改成直接解析我们自己的 SSE
（`/chat` 的帧）；届时本模块退役，或只保留 `GET /models` 这种"前端启动要用的探针"。

M0 的验收：打开 `/ui/`，发一句话，能看到**思考块 + 流式正文（含代码块）**逐字出现。
这里故意带一段 Python 代码块，用来验证它的 Markdown/高亮渲染链路没坏。

协议要点（对齐 `chat-demo/src/services/api/parseEvent.ts` 的校验）：

- `message_start`：`{"message": {...}}`（`usage` 可选，四个 key 必须都是数字）
- `content_block_start`：`{index, content_block: {type: 'text'|'thinking', ...}}`
- `content_block_delta`：`{index, delta: {type: 'text_delta', text} | {type: 'thinking_delta', thinking}}`
- `content_block_stop`：`{index}`
- `message_delta`：`{delta: {stop_reason: 'end_turn'|'max_tokens'|'tool_use'|'refusal'}}`
- `message_stop`
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()

# 每帧之间歇一下，肉眼能看到"流"（真实后端是模型延迟，这里纯粹是演示）
_FRAME_DELAY_S = 0.012


def _sse(event: dict) -> str:
    """一帧 Anthropic 风格的 SSE（`event:` 行与 `data:` 行都给，兼容两种读法）。"""
    body = json.dumps(event, ensure_ascii=False)
    return f'event: {event["type"]}\ndata: {body}\n\n'


def _usage(input_tokens: int = 0, output_tokens: int = 0) -> dict:
    return {
        'input_tokens': input_tokens,
        'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': 0,
        'output_tokens': output_tokens,
    }


def _last_user_text(body: dict) -> str:
    """从 Anthropic 请求体里取最后一条用户文本（`content` 可能是字符串或块数组）。"""
    for message in reversed(body.get('messages') or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # 只收字符串 text：坏客户端可能把 text 传成 null/数字（OCR 审核发现）
            texts = [block['text'] for block in content
                     if isinstance(block, dict) and block.get('type') == 'text'
                     and isinstance(block.get('text'), str)]
            if texts:
                return ''.join(texts)
    return ''


def _mock_reply(question: str) -> tuple[str, str]:
    """(思考文本, 正文) —— 正文带代码块，用来验证 Markdown/高亮渲染。"""
    short = question.strip().replace('\n', ' ')[:40]
    thinking = (
        f'用户在说：{short}。\n'
        'M0 阶段的回复是**固定模板**：只为验证新前端的流式、思考块与代码块渲染。\n'
        'M1 会把这里换成 my_coder 真正的 agent 流（工具调用、审批、后台任务）。'
    )
    text = (
        f'你好，这里是 **MyCoder 的新前端**（视觉层 fork 自 chat-demo）。\n\n'
        f'你刚才说的是：`{short}`\n\n'
        '## 这是 mock 后端\n\n'
        '当前 `POST /anthropic/v1/messages` 由 `my_coder/web/anthropic.py` 返回固定内容，'
        '**还没接我们的 agent**。M1 会把它换成真实的流：\n\n'
        '```python\n'
        'async def chat(request: Request) -> StreamingResponse:\n'
        '    # 我们的 SSE：assistant/chunk、tool/call、tool/result、approval/request ...\n'
        '    yield f"data: {json.dumps(event)}\\n\\n"\n'
        '```\n\n'
        '| 阶段 | 状态 |\n|---|---|\n'
        '| M0 视觉壳 + mock 流 | ✅ 当前 |\n'
        '| M1 接通真实 agent 流 | 待做 |\n'
        '| M2 工具卡 / 审批按钮 | 待做 |\n'
        '| M3 会话侧栏 / 附件 / 语音 | 待做 |\n'
    )
    return thinking, text


async def _frames(question: str) -> AsyncIterator[str]:
    message_id = f'msg_{uuid.uuid4().hex[:16]}'
    model = 'my-coder-mock'
    thinking, text = _mock_reply(question)

    yield _sse({
        'type': 'message_start',
        'message': {
            'id': message_id, 'type': 'message', 'role': 'assistant', 'model': model,
            'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': _usage(),
        },
    })
    # ① 思考块（index 0）
    yield _sse({'type': 'content_block_start', 'index': 0,
                'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}})
    for piece in _chunks(thinking, 12):
        await asyncio.sleep(_FRAME_DELAY_S)
        yield _sse({'type': 'content_block_delta', 'index': 0,
                    'delta': {'type': 'thinking_delta', 'thinking': piece}})
    yield _sse({'type': 'content_block_stop', 'index': 0})
    # ② 正文块（index 1）
    yield _sse({'type': 'content_block_start', 'index': 1,
                'content_block': {'type': 'text', 'text': ''}})
    for piece in _chunks(text, 10):
        await asyncio.sleep(_FRAME_DELAY_S)
        yield _sse({'type': 'content_block_delta', 'index': 1,
                    'delta': {'type': 'text_delta', 'text': piece}})
    yield _sse({'type': 'content_block_stop', 'index': 1})
    yield _sse({
        'type': 'message_delta',
        'delta': {'stop_reason': 'end_turn', 'stop_sequence': None,
                  'title': 'MyCoder · M0 mock'},
        'usage': _usage(len(question), len(text)),
    })
    yield _sse({'type': 'message_stop'})


def _chunks(text: str, size: int) -> list[str]:
    """按字符切块（中文按字切，不会切开一个多字节字符——Python str 本来就是码点）。"""
    return [text[i:i + size] for i in range(0, len(text), size)] or ['']


@router.get('/models')
def models() -> JSONResponse:
    """前端启动时用它校验 key（非 2xx 就报错）。M0 无所谓 key，返回一个模型。"""
    return JSONResponse({
        'object': 'list',
        'data': [{'id': 'my-coder', 'object': 'model', 'created': int(time.time()),
                  'owned_by': 'my-coder'}],
    })


@router.post('/anthropic/v1/messages')
async def messages(request: Request) -> StreamingResponse:
    """M0：把请求体里的最后一句用户输入当成话题，回一段固定内容（流式）。"""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        # JSONDecodeError 是 ValueError 的子类；空 body / Content-Type 不匹配也走这里，
        # 一律回退成空 body（宽容解析是本 mock 的本意，不该 500）
        body = {}
    if not isinstance(body, dict):
        body = {}
    question = _last_user_text(body)
    return StreamingResponse(_frames(question), media_type='text/event-stream',
                             headers={'cache-control': 'no-cache', 'x-accel-buffering': 'no'})
