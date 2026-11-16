# Agent Note: 工具级异步调用（长 I/O 不再堵住这一轮）

Status: implemented

## Problem

**ReAct 循环是同步的：一个慢工具会把整个 step 停住，等超时后活儿白干。**

`my_coder/runtime/loop.py::_execute_tool_calls` 能在**同一 step 内**并发多个工具
（`execution_mode='parallel'` → `create_task` + `gather`），每个工具还有 `timeout_s`
（默认 **60 s**）。于是"读一个很大的文件 / 搜一棵很大的树 / 一次慢的联网搜索"只有两种结局：

1. 等到它返回 —— 这 60 s 里**模型在原地等**；
2. 等满超时 —— 返回超时 `is_error`，**已经跑掉的时间白费**。

`offload=True` 解决的是另一件事（把**同步阻塞**挪出事件循环，别卡住别的会话），它**不解除**
"这一轮必须等这个工具"的约束。所以缺的不是并发度，是**"先接着干、结果稍后到"的协议**。

## Decision

### 1. 声明（`state/registry.py::ToolSpec`）

```python
detachable: bool = False             # 允许异步化（默认 False，fail-closed）
detach_after_s: float | None = None  # 前台超过这个秒数就转后台（None = 不转）
```

**只有"I/O 重、无副作用"的工具声明**：现在 `read_file` 声明了（`detachable=True`,
`detach_after_s=3.0`, `my_coder/tools/file_io.py`）。写类工具（`bash` / `edit` / `write_file`）
**不声明**——后台命令与前台命令改同一批文件是竞态，不是性能问题。
`execute(..., timeout_s=…)` 新增一个通道：传 `None` 表示**调用方自管超时**（后台任务与
"超时转后台"都要用它，否则后台任务跑到 60 s 会被误判成失败）。

### 2. 两条路径（`runtime/loop.py::_run_one`）

- **显式后台**：`run_in_background=true` ⇒ 立刻返回
  `started background job job-7f3a (read_file)` 的 `tool/result`
  （**协议仍完整**：每个 `tool_call` 都有结果），任务交给 `state/jobs.py::JobRegistry`。
- **前台超时转后台**：`asyncio.wait_for(asyncio.shield(task), detach_after_s)` 到点后
  **只取消 shield、不取消任务**，把它 `adopt` 成后台任务，结果文本写
  `still running after 3.0s — moved to background job job-… (NOT cancelled)`。
  这是 DSH `enableForegroundTimeoutDetach` 的做法：**活不白干**。

### 3. 三个工具（`my_coder/tools/jobs.py`，形制照 DSH `packages/jobs/tool-jobs`）

| 工具 | 语义 |
|---|---|
| `job_list` | **本会话**的任务：`id · 状态 · 工具 · 耗时 · 输出字节`；状态闭集 `running / stopping / completed / killed / failed` |
| `job_output` | **增量输出**（自上次读起）+ `lossy` 标记；`wait=true` 时**有界等待**（`timeout_ms`，默认 10 s，上限 60 s）——反轮询的正解 |
| `job_kill` | 请求停止：状态先 `stopping`，settle 后 `killed`；重复 kill 是 `is_error` |

三个工具都 `cacheable=False`：**不许**把"反复 job_output"算成合法的只读探索（绕开工具收敛）。

### 4. 交付与记账

- **审计**：`tool/background/start`（起任务）与 `tool/background/result`
  （`status / detail / output / lossy / elapsed_ms`）两个**痕迹事件**（`ignorable=True`，
  词汇增长不 bump 版本）；
- **模型可见**：settle 时 `agent.followup(...)` 往 inbox **splice 一条通知**（持久化，符合
  "入队即记账"），文本形如
  `[后台任务 job-7f3a completed · 4.2s · read_file] completed。用 job_output 读输出；job_list 看全部后台任务。`
  ——**必须点名 `job_output`**，否则模型不知道下一步干什么；
- **状态栏**：`state/jobs.py::build_jobs_status` 注册为运行时状态贡献者（`app/factory.py` 一行），
  形如 `⏳ 后台任务 1 个：job-7f3a · read_file · a.txt`；
  **它是日志投影**（判据：有 `start`、没有对应 `result`）——不读内存注册表，
  否则就是第二份状态（违反不变式①）。
- **崩溃恢复**：`state/recovery.py::repair_background_jobs` 把"有 start、没 result"的任务补成
  `killed / process restarted before the job settled`，由 `repair_dangling_tool_calls` 每个入口调用
  一次（幂等）。

### 5. 上限

每会话并发后台任务 v1 未设硬上限（`JobRegistry` 全进程一份）；单任务输出 **64 KB**，超出截断并打
`lossy`。并发上限与"输出游标持久化"留作后续（见 Consequences）。

## Alternatives

- **子 Agent**（DSH `subagent` / opencode `task`）：能解决长任务，但开的是**新会话 + 新 ReAct 循环**，
  产物是"另一个上下文里的回答"，不是"这个工具调用的结果"。性质不同，留作后续能力。
- **只靠 `offload=True`**：只保证不卡事件循环，**模型仍在等**。否决。
- **调大 `timeout_s`**：等更久、白干更久。否决。
- **只给 `job_output` 轮询、不 splice 通知**：模型会忘、会刷（opencode 的文案就是为此写的）。否决。
- **让 `bash` 也能 detach**：后台命令与前台命令的写竞态无法用声明解决。否决，等写类工具的隔离另议。
- **把 Job 状态持久化到磁盘**：日志已经是事实源，再存一份必然出现"内存说 running、日志说 killed"。否决。

## Consequences

- **宿主的事件循环必须长活**：后台任务活在 loop 里，`asyncio.run(...)` 结束时会把未完成任务
  全部取消。Web 宿主是一个长活循环 ✓；CLI 一个进程一个循环 ✓；但**"每个 turn 起一个新 loop"**
  的写法会让后台任务在 turn 结束时死掉（测试里也踩到了这一点，见 Testing）。
- **重启即收口**：进程重启后任务不可能还在跑，`repair_background_jobs` 立刻把它们标 killed；
  代价是"重启后拿不到那次任务的结果"（输出只在内存），这是"日志是事实源 + 不存第二份状态"的
  直接推论。
- **`read_file` 的 detach 是"页"级别**：后台返回的仍是那一页（`offset`/`limit` 语义不变），
  不是整个文件——工具描述里必须让模型明白这一点。
- **输出游标在内存**：重启后 `job_output` 的游标重置，第一次读返回全量（64 KB 上限）。
- **未做的**：并发上限、输出游标持久化、Web 端"任务面板"（后端信息已足够：工具结果 +
  状态栏 + 完成通知）。
- **取消的边界**：取消（用户点停止）只取消**正在前台等待**的那次调用；**已经转后台的任务不会
  随 turn 一起取消**（要停请用 `job_kill`）——这是"后台任务独立于 turn"的直接推论。

## Testing

`tests/test_async_tools.py`（5 条，全部通过）：

| 判据 | 断言 |
|---|---|
| 显式后台 | 返回 `started background job job-…` 且非 `is_error`；日志出现 `tool/background/start` |
| settle | 任务 `completed`、输出含文件内容；日志出现 `tool/background/result`；通知里**点名 `job_output`** |
| 超时转后台 | 结果文本含 `moved to background job` 与 `(NOT cancelled)`；任务最终 `completed`、输出是慢调用的结果 |
| `job_output` | 第一次读到输出、第二次读 `no new output`（增量）；已结束任务的 `wait=true` 立刻返回；未知 id `is_error` |
| `job_list` / `job_kill` | 列表含 id 与 `running`；kill 后任务 `killed`；重复 kill `is_error` |
| 状态栏 + 恢复 | 无任务时 `None`；有 start 无 result 时显示；`repair_background_jobs` 补 `killed` 且幂等 |

另有一轮 **`ocr`（open-code-review）审阅**（对提交 `75c38b5`，10 文件 / 14 条评论）抓到并修掉 5 处：

1. `repair_background_jobs` 曾被放在 `if not calls: return ()` **之后** ⇒ "只有孤儿后台任务"的
   会话永远收不了口（状态栏永久在跑）；
2. 取消路径：`wait_for(shield(pending))` 只取消 shield ⇒ 底层调用成了无人 await 的孤儿任务，
   且日志里没有任何痕迹；
3. `adopt` 场景的 `job_kill` 只停包装任务 ⇒ 被接管的底层任务仍在跑；
4. `build_jobs_status` 假定 `event.data` 一定是 dict ⇒ 坏日志让整条状态栏静默消失；
5. settle 记账失败会把任务拖成"未处理异常收尾"，`tool/background/result` 永远不落。

对应的 5 条回归测试在 `tests/test_async_tools.py` 里（`test_recovery_repairs_orphan_jobs_*`、
`test_cancel_while_detaching_cancels_the_pending_tool`、`test_kill_cancels_the_adopted_task`、
`test_status_tolerates_non_dict_event_data`、`test_settle_bookkeeping_failure_does_not_break_the_job`）。

三绿：`ruff`（含 `eval`）· `mypy` · `pytest`。
