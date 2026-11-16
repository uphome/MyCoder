"""后台工具任务（工具级异步，见 docs/notes/proposed/feature/2026-11-16-async-tool-calls.md）。

**这里只有"正在跑"的句柄**——任务的身份、工具名、开始时间都会落进会话日志
（`tool/background/start` / `tool/background/result`），所以：

- 进程重启后本表是空的（内存态），日志投影仍能说明"哪些任务没结果"；
- `state/recovery.py` 在重放时把"有 start、没 result"的任务补成 `killed`，
  ⇒ 不会出现"日志说在跑、其实进程早没了"的幽灵任务。

为什么不做持久化：**日志是唯一事实源**（不变式①）。这里存第二份状态会立刻出现
"内存说 running、日志说 aborted"这种双真相。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# 状态闭集（对齐 DSH 的 JobOutcome：killed / completed / failed，外加"已请求停"）
RUNNING = 'running'
STOPPING = 'stopping'
COMPLETED = 'completed'
KILLED = 'killed'
FAILED = 'failed'

# 单个任务的输出上限（字符近似；超出截断并打 lossy 标记）
OUTPUT_LIMIT_CHARS = 64 * 1024

# 读游标/输出只留在内存：重启后游标重置，第一次 `job_output` 返回全量（有上限）
OnSettle = Callable[['Job'], None]


@dataclass
class Job:
    """一个后台工具任务。`task` 只在"这次进程里"有意义。"""

    id: str
    session_id: str
    tool: str
    label: str
    started_at: float
    status: str = RUNNING
    output: str = ''
    lossy: bool = False
    cursor: int = 0
    detail: str = ''
    task: asyncio.Task | None = None
    # `adopt` 场景下真正在跑的是这个底层 Task（`task` 只是 `_drive` 的包装）——
    # 取消时必须一并取消它，否则 job_kill 只是改了状态，前台工具还在后台跑。
    adopted: asyncio.Task | None = None

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class JobRegistry:
    """一个进程一份（挂在 `Agent.jobs` 上）；按 `session_id` 过滤给模型看。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    # ---------- 生命周期 ----------

    def start(self, *, session_id: str, tool: str, label: str,
              work: Callable[[], Awaitable[object]], on_settle: OnSettle | None = None) -> Job:
        """起一个后台任务（`work()` 返回 ToolOutcome 或字符串）。"""
        job = self._new(session_id=session_id, tool=tool, label=label)
        job.task = asyncio.create_task(self._drive(job, work, on_settle))
        return job

    def adopt(self, *, session_id: str, tool: str, label: str, task: asyncio.Task,
              on_settle: OnSettle | None = None) -> Job:
        """接管一个**已经在跑**的 Task（前台超时转后台用，任务本身绝不被取消）。"""
        job = self._new(session_id=session_id, tool=tool, label=label)
        job.adopted = task
        job.task = asyncio.create_task(self._drive(job, None, on_settle, adopted=task))
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in (RUNNING, STOPPING):
            return False
        job.status = STOPPING
        if job.task is not None:
            job.task.cancel()
        if job.adopted is not None and not job.adopted.done():
            job.adopted.cancel()      # 底层真活儿也要停（见 Job.adopted 的注释）
        return True

    # ---------- 查询 ----------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_for(self, session_id: str) -> list[Job]:
        jobs = [job for job in self._jobs.values() if job.session_id == session_id]
        return sorted(jobs, key=lambda job: job.started_at)

    async def collect(self, job_id: str, *, wait: bool = False,
                      timeout_ms: int = 10_000) -> tuple[Job | None, str]:
        """取"自上次读起"的增量输出；`wait=True` 时最多等 `timeout_ms`（有界等待）。

        返回 `(job, delta)`；job 不存在时 `(None, '')`（调用方降级成 is_error）。
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None, ''
        if wait and job.status in (RUNNING, STOPPING) and job.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(job.task), max(0.0, timeout_ms / 1000))
            except (TimeoutError, asyncio.CancelledError):
                pass          # 有界等待：到点就返回"还没完"，不是错误
        delta = job.output[job.cursor:]
        job.cursor = len(job.output)
        return job, delta

    # ---------- 内部 ----------

    def _new(self, *, session_id: str, tool: str, label: str) -> Job:
        job = Job(id=f'job-{uuid.uuid4().hex[:8]}', session_id=session_id, tool=tool,
                  label=label, started_at=time.monotonic())
        self._jobs[job.id] = job
        return job

    async def _drive(self, job: Job, work: Callable[[], Awaitable[object]] | None,
                     on_settle: OnSettle | None, *,
                     adopted: asyncio.Task | None = None) -> None:
        try:
            if adopted is not None:
                outcome = await adopted
                job.output = getattr(outcome, 'content', None) or str(outcome)
                is_error = bool(getattr(outcome, 'is_error', False))
                job.status = FAILED if is_error else COMPLETED
                job.detail = 'tool reported an error' if is_error else 'completed'
            else:
                assert work is not None
                outcome = await work()
                job.output = getattr(outcome, 'content', None) or str(outcome)
                job.status = COMPLETED
                job.detail = 'completed'
        except asyncio.CancelledError:
            job.status = KILLED
            job.detail = 'cancelled'
        except Exception as error:                       # noqa: BLE001 - 任务失败不炸进程
            job.output = f'{type(error).__name__}: {error}'
            job.status = FAILED
            job.detail = str(error)
        finally:
            if len(job.output) > OUTPUT_LIMIT_CHARS:
                job.output = job.output[:OUTPUT_LIMIT_CHARS]
                job.lossy = True
            if on_settle is not None:
                on_settle(job)


def build_jobs_status(session, *, limit: int = 3) -> str | None:
    """状态栏：**日志投影**（AGENTS.md：`build` 必须是日志投影的纯函数且便宜）。

    判据：有 `tool/background/start`、但没有对应 `tool/background/result` ⇒ 还在跑。
    进程重启后由 `state/recovery.py` 补的 `killed` 收口 ⇒ 不会出现幽灵任务。
    """
    started = [event for event in session.events
               if event.type == 'tool/background/start' and isinstance(event.data, dict)]
    if not started:
        return None
    done = {event.data.get('job_id') for event in session.events
            if event.type == 'tool/background/result' and isinstance(event.data, dict)}
    live = [event for event in started if event.data.get('job_id') not in done]
    if not live:
        return None
    shown = live[-limit:]
    rows = '；'.join(
        f"{event.data.get('job_id')} · {event.data.get('tool')}"
        + (f" · {event.data.get('label')}" if event.data.get('label') else '')
        for event in shown)
    more = f'（另有 {len(live) - len(shown)} 个）' if len(live) > len(shown) else ''
    return f'⏳ 后台任务 {len(live)} 个：{rows}{more}'
