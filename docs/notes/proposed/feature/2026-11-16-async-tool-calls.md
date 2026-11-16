# Agent Note: 工具级异步调用（长 I/O 不堵这一轮）

Status: proposed

## Problem

**ReAct 循环是同步的：一个慢工具会把整个 step 停住，等超时后活儿白干。**

现状（`my_coder/runtime/loop.py::_execute_tool_calls`）：同一 step 内的多个工具可以并发
（`execution_mode='parallel'` → `create_task` + `gather`），每个工具还有 `timeout_s`（默认 **60 s**，
`my_coder/state/registry.py::ToolSpec`）。于是"读一个很大的文件 / 搜一棵很大的树 / 一次慢的联网搜索"
只有两种结局：

1. 等到它返回 —— 这 60 s 里**模型在原地等**（它没法先做别的事，也没法把这件事挪到后台）；
2. 等满超时 —— 返回一条超时的 `is_error`，**已经跑掉的时间白费**，重试还要再等一遍。

`offload=True` 解决的是另一件事（把**同步阻塞**挪出事件循环，别卡住别的会话），它**不解除**"这一轮
必须等这个工具"的约束。所以这不是"并发度不够"，而是**缺一个"先接着干、结果稍后到"的协议**。

## 三家开源实现怎么做的（本地源码核对）

| 项目 | 机制 | 关键设计 |
|---|---|---|
| **DSH** | `tool-bash` 的 `run_in_background` + 通用 jobs（`packages/jobs/tool-jobs` → `job_output` / `job_kill`） | ① 后台调用**立刻返回 job id**；② **前台超时会"转后台"而不是杀掉**（`enableForegroundTimeoutDetach` 默认 true，`packages/shell/tool-bash/src/index.ts`）；③ 输出是**字节游标拉取**（`read(fromByte) → {text, nextOffset, lossy}`，`packages/shell/tool-bash/src/background.ts`），`lossy` 标记环形缓冲溢出；④ 终态 `JobOutcome{status: killed/completed/failed, detail}`，**非零退出码算 completed** 而不是 failed |
| **opencode** | `shell.ts` + `BackgroundJob` 服务（`packages/opencode/src/tool/shell.ts`、`src/background/job.ts`） | 后台启动即返回；**完成后自动通知**；工具文案明确教模型"**不要 sleep / 轮询 / 重复干活**"（`BACKGROUND_STARTED`） |
| **PI** | 无工具级后台（只有并行的**子进程子任务**：`examples/extensions/subagent/index.ts`） | — |

结论：**工具级异步是 shell/jobs 层的能力，不是 subagent 层**；DSH 的两条细则（前台超时转后台、
输出带 `lossy`）值得照搬。

## Proposal

### 1. 声明（`state/registry.py::ToolSpec`）

```python
ToolSpec(
    ...,
    detachable: bool = False,             # 允许异步化（默认 False，fail-closed）
    detach_after_s: float | None = None,  # 前台超过这个秒数就转后台（None = 不转）
)
```

**v1 只给"I/O 重、无副作用"的工具**：先 `read_file`，随后同批铺开 `grep` / `glob` / `web_search` /
`session_manifest` / `read_turn`。**写类工具（`bash` / `edit` / `write_file`）v1 不给**——后台命令与前台
命令可能改同一批文件，那是竞态，不是性能问题（见 Alternatives）。

### 2. 参数（只在 `detachable` 工具上出现在 schema 里）

```jsonc
{ "run_in_background": true }   // 默认 false；前台行为完全不变
```

工具描述必须写清（抄 opencode 的反轮询文案）：后台模式**立刻返回 job id**，**不要轮询、不要重复
这批工作**，完成时你会收到一条消息；只有"现在就要看部分输出"才用 `job_output`。

### 3. 执行（`runtime/loop.py`）

- **后台**：立刻落一条 `tool/result` = `started background job job-7f3a（完成时会以消息通知；job_output 取、job_kill 停）`
  ⇒ **协议仍然合法**（每个 `tool_call` 都有结果），然后把 `executor` 包成 `asyncio.Task` 交给 Job 注册表。
- **前台超时 → 转后台（已定）**：`asyncio.wait_for` 到 `detach_after_s` 后**不取消任务**，改落
  "仍在跑，已转后台 job-…"，循环继续 ⇒ **不丢活儿**。
- **取消**：`job_kill` → `task.cancel()` → 沿用现有 `CancelledError` 单向传播，记 `is_error`。

### 4. 结果交付（两个通道，都要）

- **日志（审计 + 可重建）**：`tool/background/result {job_id, status, output, lossy, elapsed_ms}`；
- **模型可见（已定：直接 splice）**：完成时往该会话 **inbox splice 一条消息**（`queued` 分区，持久化 ✓
  符合"入队即记账"）：`[后台任务 job-7f3a 完成 · 12.4s] read_file 输出（前 4 KB）：…`
- **崩溃恢复**：有 `tool/result`（started）但没有对应的 `tool/background/result` ⇒ 重放时补一条
  `status:'aborted', detail:'process restarted'`（与 `state/recovery.py::repair_dangling_tool_calls` 同一模式）
  ⇒ 不留"永远在跑"的幽灵任务。

### 5. 供给面（AGENTS.md 硬要求：新工具 = 实现 + 供给面）

- 新工具 **`job_output(job_id)`**（取当前输出；v1 返回**全量但有上限**，不做字节游标；文档写明
  "重复调用返回同样的内容"）与 **`job_kill(job_id)`**；
- **运行时状态贡献者**（`app/factory.py` 一行）：`⏳ 1 个后台任务在跑：job-7f3a（read_file, 8.2s）`
  ⇒ 模型不用猜、也不容易忘；
- **上限**：每会话并发后台任务 **2**；单任务输出 **64 KB**（超出截断并打 `lossy: true`）；超限返回
  `is_error` 并说明理由（fail-closed）。

## Alternatives

- **子 Agent（DSH `subagent` / opencode `task`）**：能解决"长任务"，但性质不同——它开的是**新会话 +
  新 ReAct 循环**，产物是"另一个上下文里的回答"，而不是"这个工具调用的结果"。实现成本高一档
  （子日志、深度上限、工具过滤、模型选择）。**留作后续能力**，不替代本项。
- **只把 `offload=True` 用起来**：它只保证"不卡事件循环"，**模型仍在等**。否决。
- **单纯调大 `timeout_s`**：等更久、白干更久，且长尾会顶满上下文窗口的等待时间。否决。
- **只给 `job_output` 轮询、不 splice 通知**：模型会忘、会刷（opencode 的反轮询文案正是为这个写的）。否决。
- **v1 就让 `bash` 也能 detach**：后台命令与前台命令写同一批文件的竞态无法用声明解决
  （`execution_mode` 只约束同一 step 内）。否决，等"写类工具的隔离"另议。

## Risks

- **模型 spam `job_output`**：靠"完成自动通知 + 状态栏 + 文案禁止轮询"三道；再加"同一 job 的
  `job_output` 结果进 `cacheable=False`（不算只读调用）"以免绕过收敛判据。
- **输出膨胀**：64 KB 上限 + `lossy` 标记（照 DSH）；`job_output` 也只返回这一份。
- **同一文件并发两个 detached 读**：并发上限 2 + 内存峰值（每任务一次读），可接受；超出即 `is_error`。
- **`read_file` 的分页语义**：detached 返回的仍是**那一页**（不是整个文件）——文案要说清，否则模型
  会以为"后台读完 = 拿到全文"。
- **孤儿任务**：靠"重放补 aborted"兜底；不会出现"日志说在跑、其实进程早没了"。

## Acceptance

落地的判据（实施时逐条写成测试）：

- [ ] `read_file` 声明 `detachable`，带 `run_in_background=true` 时**立即**返回 `job-…`，且该
      `tool/result` 合法（协议不断）
- [ ] 后台任务完成后：会话 **inbox 里出现一条完成消息**（`queued` 分区）+ 日志有 `tool/background/result`
- [ ] 前台超过 `detach_after_s`：结果里写着"已转后台 job-…"，**任务没有被取消**，之后仍能收到完成通知
- [ ] `job_kill` 之后：任务被取消、日志记 `status:'killed'`，父会话不受影响
- [ ] 重放一个"started 但没有 result"的日志：自动补 `aborted`（不炸、不悬空）
- [ ] 并发上限 2 与输出上限 64 KB（`lossy`）各有测试
- [ ] 三绿（`ruff` 含 `eval` · `mypy` · `pytest`）+ 真机演示：读一个大会话/大文件时，这一轮**不再卡**
