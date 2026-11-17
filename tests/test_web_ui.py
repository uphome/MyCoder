"""新前端（`web/ui`，视觉层 fork 自 chat-demo）的 M0 契约。

M0 的判断标准只有三条，但都必须钉住：
1. `/ui/` 能提供构建产物（没构建过时给**明确指引**，不是裸 404）；
2. `GET /models` 是前端启动探针，形状对（非 2xx 会被前端当成 key 无效）；
3. `POST /anthropic/v1/messages` 吐的 SSE 帧**顺序与字段**符合前端的解析器
   （`chat-demo/src/services/api/parseEvent.ts` 的校验），否则流会被静默丢弃。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from my_coder import web

_ROOT = Path(__file__).resolve().parents[1]
_DIST_INDEX = _ROOT / 'web' / 'ui' / 'dist' / 'index.html'


def _client() -> TestClient:
    return TestClient(web.app)


def _frames(text: str) -> list[dict]:
    """把 SSE 响应体切成事件对象（只取 data 行）。"""
    events = []
    for line in text.splitlines():
        if line.startswith('data: '):
            events.append(json.loads(line[len('data: '):]))
    return events


def test_ui_is_served_or_explains_how_to_build():
    client = _client()
    response = client.get('/ui/')
    if not _DIST_INDEX.is_file():
        assert response.status_code == 404
        assert 'pnpm build' in response.text, '没构建时必须告诉人怎么构建（不要裸 404）'
        pytest.skip('web/ui/dist 未构建')
    assert response.status_code == 200
    assert 'id="root"' in response.text, 'React 挂载点必须在'


def test_ui_ships_relative_asset_paths():
    """产物必须用**相对**路径：托管在 /ui/ 下时 `static/...` 能正确解析。"""
    if not _DIST_INDEX.is_file():
        pytest.skip('web/ui/dist 未构建')
    html = _DIST_INDEX.read_text(encoding='utf-8')
    assert 'src="static/js/main' in html, html
    assert 'src="/static/' not in html, '绝对路径会在 /ui/ 前缀下 404'


def test_models_probe_shape():
    response = _client().get('/models')
    assert response.status_code == 200
    body = response.json()
    assert body['object'] == 'list'
    assert body['data'] and 'id' in body['data'][0]


def test_messages_streams_anthropic_wire_order():
    """帧序：message_start → start/delta/stop ×2 块 → message_delta → message_stop。"""
    client = _client()
    response = client.post('/anthropic/v1/messages', json={
        'model': 'my-coder', 'stream': True, 'max_tokens': 256,
        'messages': [{'role': 'user', 'content': '你好'}],
    })
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')

    events = _frames(response.text)
    types = [event['type'] for event in events]
    assert types[0] == 'message_start'
    assert types[-2] == 'message_delta'
    assert types[-1] == 'message_stop'
    assert types.count('content_block_start') == 2, '思考块 + 正文块'
    assert types.count('content_block_stop') == 2

    blocks = [event['content_block']['type'] for event in events
              if event['type'] == 'content_block_start']
    assert blocks == ['thinking', 'text']

    assert events[-2]['delta']['stop_reason'] == 'end_turn'
    # 中文必须原样往返（别被编码问题吃掉）
    text = ''.join(event['delta'].get('text', '') for event in events
                   if event['type'] == 'content_block_delta')
    assert '你好' in text
