"""工具级异步调用的判据（docs/notes/proposed/feature/2026-11-16-async-tool-calls.md）。

**一个前提**：后台任务活在**事件循环**里——`asyncio.run` 结束时会取消所有未完成任务，
所以每个场景必须把"起任务 + 等它 settle"放在**同一个** loop 里（这也正是宿主的真实形态：
Web 是一个长活循环；CLI 一个进程一个循环）。

六个面：
1. 显式 `run_in_background`：立刻返回 job id（协议仍完整），日志落 `tool/background/start`；
2. settle：日志落 `tool/background/result`，并往 inbox 塞一条**点名 `job_output`** 的通知；
3. 前台超时**转后台**：任务**不取消**，最终 completed；
4. `job_output` 是**增量**，支持**有界等待**；未知 id 是 `is_error`；
5. `job_kill` 停掉运行中的任务（状态收口 killed），重复 kill 是 `is_error`；
6. 状态栏是**日志投影**；恢复把"有 start 没 result"补成 killed 且幂等。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from my_coder.runtime.loop import _run_one
from my_coder.state.jobs import JobRegistry, build_jobs_status
from my_coder.state.recovery import repair_background_jobs
from my_coder.state.registry import ToolSpec
from my_coder.state.session import Session
from my_coder.tools import build_tools
from my_coder.values.messages import ToolCallBlock, ToolOutcome


def _agent(tmp_path, *, extra_spec: ToolSpec | None = None):
    session = Session(id='jobs-test')
    tools = build_tools(tmp_path)
    if extra_spec is not None:
        tools.register(extra_spec)
    notices: list[str] = []
    agent = SimpleNamespace(
        session=session,
        tools=tools,
        jobs=JobRegistry(),
        followup=lambda text, rpc_id='': notices.append(text),
    )
    return agent, notices


def _call(call_id: str, name: str, **arguments) -> ToolCallBlock:
    return ToolCallBlock(id=call_id, name=name, arguments=json.dumps(arguments))


def _text(message) -> str:
    return message.content[0].content


def test_background_read_returns_job_id_then_settles(tmp_path):
    (tmp_path / 'a.txt').write_text('hello\n' * 10, encoding='utf-8')
    agent, notices = _agent(tmp_path)

    async def scenario():
        message = await _run_one(agent, _call('c1', 'read_file', file_path='a.txt',
                                             run_in_background=True))
        job = agent.jobs.list_for(agent.session.id)[0]
        await asyncio.wait_for(asyncio.shield(job.task), 5)   # 同一个 loop 里等它 settle
        return message, job

    message, job = asyncio.run(scenario())

    text = _text(message)
    assert 'started background job job-' in text, text
    assert not message.content[0].is_error
    types = [event.type for event in agent.session.events]
    assert 'tool/background/start' in types, '起任务必须落痕迹（状态栏/恢复都靠它）'
    assert 'tool/background/result' in types, 'settle 必须落审计事件'
    assert job.status == 'completed', job.detail
    assert 'hello' in job.output
    assert notices and 'job_output' in notices[0], (
        '完成通知必须点名 job_output（否则模型不知道该干嘛）')


def test_foreground_timeout_moves_to_background_without_cancelling(tmp_path):
    async def slow(arguments, agent, signal):
        await asyncio.sleep(0.05)
        return ToolOutcome('slow done')

    agent, notices = _agent(tmp_path, extra_spec=ToolSpec(
        name='slow_tool', description='slow', parameters={'type': 'object', 'properties': {}},
        execute=slow, detachable=True, detach_after_s=0.01))

    async def scenario():
        message = await _run_one(agent, _call('c2', 'slow_tool'))
        job = agent.jobs.list_for(agent.session.id)[0]
        await asyncio.wait_for(asyncio.shield(job.task), 5)
        return message, job

    message, job = asyncio.run(scenario())
    text = _text(message)
    assert 'moved to background job job-' in text, text
    assert '(NOT cancelled)' in text, '超时转后台的关键承诺：任务没被取消'
    assert job.status == 'completed', job.detail
    assert 'slow done' in job.output
    assert notices and 'completed' in notices[0]


def test_job_output_is_incremental_and_honours_bounded_wait(tmp_path):
    agent, _ = _agent(tmp_path)

    async def scenario():
        job = agent.jobs.start(session_id=agent.session.id, tool='fake', label='x',
                               work=lambda: _done('part A'))
        await asyncio.wait_for(asyncio.shield(job.task), 5)
        spec = agent.tools.get('job_output')
        first = await spec.execute({'job_id': job.id}, agent, None)
        second = await spec.execute({'job_id': job.id}, agent, None)
        quick = await spec.execute({'job_id': job.id, 'wait': True, 'timeout_ms': 5000},
                                   agent, None)
        unknown = await spec.execute({'job_id': 'job-nope'}, agent, None)
        return first, second, quick, unknown

    first, second, quick, unknown = asyncio.run(scenario())
    assert 'part A' in first.content
    assert 'no new output' in second.content, '第二次读必须是增量（没有新输出）'
    assert 'no new output' in quick.content, '已结束的任务有界等待应立刻返回'
    assert unknown.is_error and 'unknown job' in unknown.content


async def _done(text: str) -> ToolOutcome:
    return ToolOutcome(text)


def test_job_list_and_kill(tmp_path):
    agent, _ = _agent(tmp_path)

    async def scenario():
        job = agent.jobs.start(session_id=agent.session.id, tool='fake', label='slow',
                               work=lambda: asyncio.sleep(10))
        await asyncio.sleep(0)                       # 让任务真正开跑
        listed = await agent.tools.get('job_list').execute({}, agent, None)
        killed = await agent.tools.get('job_kill').execute({'job_id': job.id}, agent, None)
        try:
            await asyncio.wait_for(asyncio.shield(job.task), 5)
        except asyncio.CancelledError:
            pass
        again = await agent.tools.get('job_kill').execute({'job_id': job.id}, agent, None)
        return job, listed, killed, again

    job, listed, killed, again = asyncio.run(scenario())
    assert job.id in listed.content and 'running' in listed.content
    assert not killed.is_error and 'stop requested' in killed.content
    assert job.status == 'killed', job.status
    assert again.is_error, '重复 kill 必须是 is_error（不能假装成功）'


def test_status_is_a_log_projection_and_recovery_closes_orphans(tmp_path):
    agent, _ = _agent(tmp_path)
    session = agent.session
    assert build_jobs_status(session) is None, '没有任务时状态栏必须是 None（不贴空行）'

    session.append('tool/background/start',
                   {'job_id': 'job-abc123', 'call_id': 'c9', 'tool': 'read_file', 'label': 'a.txt'},
                   ignorable=True)
    status = build_jobs_status(session)
    assert status is not None and 'job-abc123' in status and 'read_file' in status

    repaired = repair_background_jobs(session)
    assert repaired == ('job-abc123',), '重启后必须把悬空任务收口'
    assert build_jobs_status(session) is None, '收口后状态栏不该再显示它在跑'
    assert repair_background_jobs(session) == (), '恢复必须幂等'

    statuses = [event.data.get('status') for event in session.events
                if event.type == 'tool/background/result']
    assert statuses == ['killed']
