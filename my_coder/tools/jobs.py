"""后台任务的三个工具：`job_list` / `job_output` / `job_kill`。

形制照 DSH 的 `packages/jobs/tool-jobs`（`job_output`、`job_list`、`job_kill`），
设计记录见 `docs/notes/proposed/feature/2026-11-16-async-tool-calls.md`：

- **`job_list`**：只列**本会话**的任务（`session_id` 过滤），状态落闭集；
- **`job_output`**：取"自上次读起"的增量；`wait=true` 时**有界等待**（最多 `timeout_ms`）——
  这是"反轮询"的正解：模型可以"等一会儿拿结果"，而不是自己刷；
- **`job_kill`**：请求停止（状态先 `stopping`，settle 后 `killed`）。

三个工具的 `cacheable` 都是 `False`：它们**不算只读调用**，不许绕开工具收敛的判据
（否则"反复 job_output"会被当成合法的只读探索）。
"""

from __future__ import annotations

from ..state.registry import ToolSpec
from ..values.messages import ToolOutcome

_STATUSES = 'running / stopping / completed / killed / failed'
# 有界等待的上限与工具的 timeout_s 绑成一处，避免两处硬编码漂移
_WAIT_MIN_MS = 0
_WAIT_MAX_MS = 60_000


def register(registry) -> None:
    async def job_list(args, agent, signal):
        jobs = agent.jobs.list_for(agent.session.id)
        if not jobs:
            return ToolOutcome('no background jobs in this session')
        lines = [
            f'{job.id}  {job.status:<9} {job.tool:<12} {job.elapsed():6.1f}s  '
            f'{len(job.output):>6} chars{" (lossy)" if job.lossy else ""}'
            for job in jobs
        ]
        return ToolOutcome('background jobs (this session):\n' + '\n'.join(lines))

    async def job_output(args, agent, signal):
        job_id = str(args.get('job_id', '')).strip()
        if not job_id:
            return ToolOutcome('job_id must not be empty', is_error=True)
        try:
            timeout_ms = int(args.get('timeout_ms', 10_000))
        except (TypeError, ValueError):
            return ToolOutcome('timeout_ms must be an integer', is_error=True)
        if timeout_ms < _WAIT_MIN_MS or timeout_ms > _WAIT_MAX_MS:
            return ToolOutcome(
                f'timeout_ms must be within [{_WAIT_MIN_MS}, {_WAIT_MAX_MS}]', is_error=True)
        # `_validate_arguments` 只查"必填齐全 + 不许多余"，**不做类型校验**——所以这里必须
        # 严格解析：`bool("false")` 是 True，会让一次本该立即返回的调用意外阻塞 60 s。
        raw_wait = args.get('wait', False)
        if isinstance(raw_wait, bool):
            wait = raw_wait
        elif isinstance(raw_wait, str) and raw_wait.strip().lower() in ('true', '1', 'yes'):
            wait = True
        elif isinstance(raw_wait, str) and raw_wait.strip().lower() in ('false', '0', 'no'):
            wait = False
        else:
            return ToolOutcome('wait must be a boolean (true/false)', is_error=True)
        job, delta = await agent.jobs.collect(job_id, wait=wait, timeout_ms=timeout_ms)
        if job is None:
            return ToolOutcome(
                f'unknown job {job_id!r} (it may have settled in a previous process; '
                'see the completion notice, or read_turn to recover the original output)',
                is_error=True)
        head = (f'job {job.id} · {job.status} · {job.elapsed():.1f}s · {job.tool}'
                f'{"  [lossy: output truncated]" if job.lossy else ""}'
                f'{"  detail: " + job.detail if job.detail else ""}')
        if not delta:
            return ToolOutcome(f'{head}\n(no new output since the last read)')
        return ToolOutcome(f'{head}\n{delta}')

    async def job_kill(args, agent, signal):
        job_id = str(args.get('job_id', '')).strip()
        if not job_id:
            return ToolOutcome('job_id must not be empty', is_error=True)
        if not agent.jobs.cancel(job_id):
            return ToolOutcome(
                f'cannot kill {job_id!r}: unknown, or already settled '
                f'(statuses: {_STATUSES})', is_error=True)
        return ToolOutcome(f'stop requested for {job_id} (status will settle to killed)')

    registry.register(ToolSpec(
        name='job_list',
        description=('List background tool jobs of THIS session: id, status, tool, elapsed, '
                     f'output size. Statuses: {_STATUSES}. '
                     'Background jobs are started by tools that accept run_in_background.'),
        parameters={'type': 'object', 'properties': {}},
        execute=job_list,
        execution_mode='parallel',
    ))
    registry.register(ToolSpec(
        name='job_output',
        description=('Read the incremental output of a background job (only the part you have not '
                     'read yet). Pass wait=true to block up to timeout_ms (default 10000) for new '
                     'output instead of polling in a loop. Do NOT poll: a completion notice is '
                     'delivered to you automatically when the job settles.'),
        parameters={
            'type': 'object',
            'properties': {
                'job_id': {'type': 'string', 'description': 'job id (job-xxxxxxxx)'},
                'wait': {'type': 'boolean',
                         'description': 'wait (bounded) for new output, default false'},
                'timeout_ms': {'type': 'integer',
                               'description': 'bounded wait in ms, default 10000, max 60000'},
            },
            'required': ['job_id'],
        },
        execute=job_output,
        execution_mode='parallel',
        timeout_s=_WAIT_MAX_MS / 1000 + 10,
    ))
    registry.register(ToolSpec(
        name='job_kill',
        description='Request that a background job stop. Status settles to killed (or completed).',
        parameters={
            'type': 'object',
            'properties': {'job_id': {'type': 'string', 'description': 'job id (job-xxxxxxxx)'}},
            'required': ['job_id'],
        },
        execute=job_kill,
        execution_mode='sequential',
    ))
