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
from my_coder.state.inbox import Inbox
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
        inbox=Inbox(session),
        # `_settle_notice` 会按 `status` 选投递方式（忙 → steer 下一步；空闲 → followup 开回合）
        status='idle',
        followup=lambda text, rpc_id='': notices.append(text),
        steer=lambda text, rpc_id='': notices.append(text),
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


# ============================================================================
# OCR 审阅（open-code-review）抓到的问题的回归测试：每条都钉住一个真实失效模式
# ============================================================================

def test_recovery_repairs_orphan_jobs_even_without_dangling_calls(tmp_path):
    """OCR #1：`repair_background_jobs` 曾被放在 `if not calls: return ()` 之后。

    于是"只有孤儿后台任务、没有悬空工具调用"的会话**永远收不了口**：状态栏永久显示在跑。
    """
    from my_coder.state.recovery import repair_dangling_tool_calls

    agent, _ = _agent(tmp_path)
    session = agent.session
    session.append('tool/background/start',
                   {'job_id': 'job-orphan', 'call_id': 'c1', 'tool': 'read_file', 'label': 'x'},
                   ignorable=True)

    assert repair_dangling_tool_calls(session) == (), '没有悬空工具调用，返回空元组'
    statuses = [event.data.get('status') for event in session.events
                if event.type == 'tool/background/result']
    assert statuses == ['killed'], '孤儿后台任务必须被收口（与悬空调用无关）'
    assert build_jobs_status(session) is None


def test_cancel_while_detaching_cancels_the_pending_tool(tmp_path):
    """OCR #2：用户取消时 `wait_for(shield(pending))` 只取消 shield，底层 Task 成了孤儿。

    修法：`except asyncio.CancelledError: pending.cancel(); raise`。这里验证底层**真的被取消**，
    且日志里**没有** `tool/background/start`（它没被接管成后台任务，不该留下痕迹）。
    """
    cancelled = asyncio.Event()

    async def slow(arguments, agent, signal):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ToolOutcome('never')

    agent, _ = _agent(tmp_path, extra_spec=ToolSpec(
        name='slow_tool', description='slow', parameters={'type': 'object', 'properties': {}},
        # `detach_after_s` 必须**大于**取消时刻：否则任务在取消前就转后台了，
        # 就测不到"取消正在前台等待的那一次调用"这条路径（第一版就踩了这个坑）。
        execute=slow, detachable=True, detach_after_s=5.0))

    async def scenario():
        task = asyncio.create_task(_run_one(agent, _call('c3', 'slow_tool')))
        await asyncio.sleep(0.05)          # 让它进入 wait_for(shield(pending))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)          # 给 pending.cancel() 生效时间
        return cancelled.is_set()

    assert asyncio.run(scenario()) is True, '取消必须传播到底层工具（否则它在后台空跑）'
    types = [event.type for event in agent.session.events]
    assert 'tool/background/start' not in types, '没转后台就不该留下后台任务痕迹'


def test_kill_cancels_the_adopted_task(tmp_path):
    """OCR #3：`adopt` 场景下 cancel 只停包装任务，底层 `read_file` 仍在跑。"""
    agent, _ = _agent(tmp_path)

    async def scenario():
        work = asyncio.create_task(asyncio.sleep(5))
        job = agent.jobs.adopt(session_id=agent.session.id, tool='read_file', label='x',
                               task=work)
        await asyncio.sleep(0)
        assert agent.jobs.cancel(job.id) is True
        try:
            await asyncio.wait_for(asyncio.shield(job.task), 2)
        except (asyncio.CancelledError, TimeoutError):
            pass
        await asyncio.sleep(0.05)
        return job, work

    job, work = asyncio.run(scenario())
    assert job.status == 'killed'
    assert work.cancelled(), '被接管的底层 Task 必须一并取消（job_kill 不能只是改状态）'


def test_status_tolerates_non_dict_event_data(tmp_path):
    """OCR #4：`build_jobs_status` 假定 `data` 一定是 dict——坏日志会让整条状态栏静默消失。"""
    agent, _ = _agent(tmp_path)
    session = agent.session
    session.append('tool/background/start', None, ignorable=True)   # 坏数据（data=None）
    session.append('tool/background/result', None, ignorable=True)
    assert build_jobs_status(session) is None, '坏数据应当被跳过，而不是把状态栏整条炸掉'


def test_settle_bookkeeping_failure_does_not_break_the_job(tmp_path):
    """OCR #5：`session.append` 失败不能让任务以未处理异常收尾（否则 result 永远不落）。"""
    agent, _ = _agent(tmp_path)
    real_append = agent.session.append

    def boom(type_, data=None, **kwargs):
        if type_ == 'tool/background/result':
            raise RuntimeError('disk full')
        return real_append(type_, data, **kwargs)

    agent.session.append = boom        # type: ignore[method-assign]

    async def scenario():
        job = agent.jobs.start(session_id=agent.session.id, tool='fake', label='x',
                               work=lambda: _done('ok'))
        await asyncio.wait_for(asyncio.shield(job.task), 5)
        return job

    job = asyncio.run(scenario())
    assert job.status == 'completed', '记账失败不该改变任务本身的成败'
    assert job.task is not None and job.task.exception() is None, '任务不许以未处理异常收尾'


def test_job_output_rejects_string_wait_and_does_not_block(tmp_path):
    """OCR 追加：`_validate_arguments` **不做类型校验**，所以 `wait="false"` 会被 `bool()` 判成 True。

    修法：严格解析（bool / 'true|1|yes' / 'false|0|no'，其余 is_error）。这里验证
    `wait="false"` 在**运行中**的任务上立即返回（不意外阻塞），非法值降级 is_error。
    """
    import time as _time

    agent, _ = _agent(tmp_path)

    async def scenario():
        job = agent.jobs.start(session_id=agent.session.id, tool='fake', label='slow',
                               work=lambda: asyncio.sleep(10))
        await asyncio.sleep(0)
        spec = agent.tools.get('job_output')
        started = _time.monotonic()
        quick = await spec.execute({'job_id': job.id, 'wait': 'false'}, agent, None)
        elapsed = _time.monotonic() - started
        bad = await spec.execute({'job_id': job.id, 'wait': 'maybe'}, agent, None)
        agent.jobs.cancel(job.id)
        return quick, elapsed, bad

    quick, elapsed, bad = asyncio.run(scenario())
    assert not quick.is_error, quick.content
    assert elapsed < 1.0, f'wait="false" 不该阻塞（实际 {elapsed:.1f}s）'
    assert bad.is_error and 'wait must be a boolean' in bad.content


def test_settle_notice_steers_while_running_and_follows_up_when_idle(tmp_path):
    """对齐 DSH 的投递语义（`packages/jobs/tool-jobs`）：忙 → 注入下一步；空闲 → 开新回合。"""
    from my_coder.runtime.loop import _settle_notice

    agent, notices = _agent(tmp_path)
    calls = {'steer': 0, 'followup': 0}

    def _steer(text, rpc_id=''):
        calls['steer'] += 1
        notices.append(text)

    def _followup(text, rpc_id=''):
        calls['followup'] += 1
        notices.append(text)

    agent.steer = _steer            # type: ignore[method-assign]
    agent.followup = _followup      # type: ignore[method-assign]
    job = SimpleNamespace(id='job-x', status='completed', detail='completed', tool='read_file',
                          label='a.txt', lossy=False, elapsed=lambda: 1.0, output='ok')

    agent.status = 'running'
    _settle_notice(agent, _call('c7', 'read_file'))(job)
    assert calls == {'steer': 1, 'followup': 0}, '忙的时候必须注入下一步（别等这一轮结束）'

    agent.status = 'idle'
    _settle_notice(agent, _call('c8', 'read_file'))(job)
    assert calls == {'steer': 1, 'followup': 1}, '空闲的时候才开新回合（DSH 的默认 wakeup）'


def test_leftover_next_step_is_carried_into_the_next_turn(tmp_path):
    """`next-step` 只在当前回合的 step≥2 被 claim；落在最后一步之后的插队消息必须顺延。

    典型场景：后台任务完成通知在回合收尾的瞬间到达。不顺延的话它会永远搁在
    `next-step` 里（下回合只 claim `next-turn`），而 `has_pending` 又一直是真。
    """
    from my_coder.capability.llm import FakeLlm
    from my_coder.runtime.agent import Agent
    from my_coder.runtime.loop import run_turn
    from my_coder.state.prompt import PromptRegistry
    from my_coder.state.registry import ToolRegistry
    from my_coder.values.messages import TextBlock, create_user_message

    session = Session(id='carry')
    agent = Agent(session=session, llm=FakeLlm([{'text': '收到', 'finish_reason': 'stop'}]),
                  prompt=PromptRegistry(), tools=ToolRegistry(),
                  options={'provider': 'fake', 'model': 'fake-model'})
    agent.inbox.append('next-step', create_user_message([TextBlock(text='后台任务完成通知')]))

    asyncio.run(run_turn(agent))

    assert not agent.inbox.next_step, '遗留的 next-step 必须被顺延并认领（不能搁死）'
    assert any(event.type == 'user/message' for event in session.events), '它要真的成为模型输入'


def test_inbox_carry_over_is_the_inverse_of_promote(tmp_path):
    """`carry_over` = `promote` 的逆操作：next-step → next-turn，且摘除那步不是"丢弃"。"""
    from my_coder.values.messages import TextBlock, create_user_message

    agent, _ = _agent(tmp_path)
    message = create_user_message([TextBlock(text='通知')])
    agent.inbox.append('next-step', message)

    assert agent.inbox.carry_over(message.id) is True
    assert agent.inbox.next_step == () and [m.id for m in agent.inbox.next_turn] == [message.id]
    assert agent.inbox.carry_over(message.id) is False, '已在 next-turn：幂等返回 False'
