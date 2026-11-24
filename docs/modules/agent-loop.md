# Agent Loop、运行模式（Normal / Plan / Goal）、HITL 与持久化续跑

本模块是 MiniCC 的核心调度引擎，负责把用户输入推进为模型请求、工具调用、人工交接（HITL）、多步规划（Plan 模式）、自主迭代（Goal 模式）以及跨进程可恢复的持久化会话。

当前行为要求见 [Agent Loop Spec](../specs/agent-loop.md)，方案取舍见 [架构重构决策记录](../decisions/implemented/architecture/2026-11-24-core-harness-refactor.md)。

## 模块结构与代码位置

| 路径 | 职责 |
| --- | --- |
| [`src/harness/agent_loop/run_turn.py`](../../src/harness/agent_loop/run_turn.py) | 基于 Generator 的 `run_turn()` 状态机，驱动模型调用、工具执行、去重、超时预算与暂停事件。 |
| [`src/runtime/session/chat_session.py`](../../src/runtime/session/chat_session.py) | 会话管理、运行模式切换（`normal`/`plan`/`goal`）、Goal 自动推进循环、HITL 恢复与 `session.json` / `checkpoint.json` 持久化。 |
| [`src/runtime/task/state.py`](../../src/runtime/task/state.py) | 任务状态 `TaskState`（`goal`、`plan_steps`、`verification_command`、`requirements`、`decisions`、`progress`、`open_questions`）与带锁原子存储 `TaskStateStore`。 |
| [`src/runtime/hitl/contracts.py`](../../src/runtime/hitl/contracts.py) | `ApprovalRequest`、`ClarificationRequest`、`RetryRequest`、`PlanStep` 及内置 `ask_user`、`update_plan` 工具。 |
| [`src/client/tui_selector.py`](../../src/client/tui_selector.py) | 基于 `prompt_toolkit` 的键盘（`↑`/`↓`/快捷键）与鼠标点选交互式 TUI 选择器及 `/` 斜杠命令自动补全。 |
| [`src/adapters/storage/json_file.py`](../../src/adapters/storage/json_file.py) | 统一的 `fcntl.flock` + `tempfile.mkstemp` + `os.replace` 原子 JSON 文件读写底座。 |

## 核心设计逻辑

### 1. Generator 状态机与 HITL 交接

`run_turn()` 返回 `Generator[TurnPause, object, str]`，其中 `TurnPause` 包含三类暂停事件：
- **`ApprovalRequest(call, preview, kind, elevated)`**：当工具 `requires_approval=True`（或请求 `allow_unsandboxed=True` 提权，或 `plan` 模式下提交计划审批）时产出。
  - 若 `elevated=True`（如沙箱外执行），无论宿主处于 `default` 还是 `auto` 模式，**强制暂停**等待用户明确批准。
  - 若 `elevated=False`，在 `auto` 模式下由宿主自动批准，在 `default` 模式下暂停交由用户选择 `yes` / `no` / `auto`。
- **`ClarificationRequest(call, question, options, allow_custom)`**：由 `ask_user` 工具产出。暂停当前轮次并将问题与候选选项展示给用户；用户回答后，答案作为该 `call_id` 的 `ToolResult` 回填模型，从断点继续当前轮次。
- **`RetryRequest(message)`**：模型请求超时或 API 异常时产出。已执行的工具结果保留在 `TurnRecord.messages` 中，用户输入 `/retry` 后从当前消息列表重新发起模型请求，不会重放已完成的副作用。

所有 HITL 暂停期间的等待时长均从本轮 `limits.seconds`（默认 900 秒）中扣除。

### 2. 三种运行模式（Normal / Plan / Goal）

`ChatSession` 维护当前模式 `mode: Literal["normal", "plan", "goal"]`（可通过 `/mode normal|plan|goal`、`/plan`、`/goal` 切换）：
- **`normal` 模式**：暴露全部可用工具，按 `default` / `auto` 权限策略执行单轮对话。
- **`plan` 模式**：
  - 工具过滤：仅向模型提供只读工具（`tool.read_only == True`，如 `read`、`glob`、`grep`、`tool_search`、`memory_search`、`load_skill`、只读 `delegate_subagent`）以及 `update_plan` 和 `ask_user`。写文件（`write`/`edit`）与命令执行（`bash`）在 `plan` 模式下不会提供给模型，执行器也会拒绝执行。
  - 计划确认：当模型调用 `update_plan(..., submit_for_approval=True)` 或完成规划时，生成结构化计划预览并触发 `ApprovalRequest(kind="plan")`。用户批准后，会话自动切入 `normal`（或用户指定的 `goal`）模式继续执行。
- **`goal` 模式**：
  - 围绕 `TaskState` 中的 `goal`、`plan_steps` 和 `verification_command` 进行多轮自主迭代（单次 `send()` / `run_goal()` 最多连续推进 `max_goal_rounds=8` 轮）。
  - 每轮结束后，若未遇到 HITL 暂停，宿主检查 `TaskState` 是否仍有未完成的 `plan_steps`（`pending` / `in_progress`），或配置的 `verification_command` 尚未验证通过。若是，则自动发起下一轮跟进请求，直至所有步骤均为 `completed` 且验证通过，或达到轮次上限。

### 3. 持久化与跨进程断点续跑（Persistence & Resume）

为了保证进程意外退出或用户切走后重进能够无缝续跑，每个项目目录 `~/.minicc/projects/<project_id>/` 维护三份通过 `atomic_write_json` 同步的文件：
1. `session.json`：已完成轮次的完整对话与工具调用轨迹（含 `session_id`、当前 `mode`）。
2. `task.json`：当前 `TaskState`（Goal、Plan 步骤清单、已确认要求与决定、验证命令）。
3. `checkpoint.json`：当一轮对话处于活动或 HITL 挂起状态（`ApprovalRequest`、`ClarificationRequest`、`RetryRequest`）时，实时保存当前轮次的 `turn_id`、已累积的 `messages`（含已完成的工具调用与结果）以及当前挂起的 `pending_pause` 描述（工具调用参数、预览或问题选项）。
   - **恢复流程（`ChatSession.resume()`）**：新进程启动加载 `ChatSession` 时，若发现存在 `checkpoint.json`，`session.has_pending_checkpoint` 为 `True`。客户端展示挂起的审批/提问/重试状态，用户调用 `resume(answer)`（或在终端输入回答 / `/resume`）即可先结算挂起项（例如执行获批的 `write`/`edit`/`bash` 或回填 `ask_user` 回答），再将已累积的本轮消息无缝接回 `run_turn()` 继续向模型请求，**既不丢失本轮前序已执行的工具结果，也不重复执行已完成的工具**。

### 4. 智能去重（解决测试循环误杀）

- 用户在本轮拒绝过的调用记录在 `denied: set[tuple[str, str]]` 中，本轮内模型若原样再次请求，直接返回 `APPROVAL_DENIED`，不再重复打扰用户。
- 只读工具若连续 3 次以完全相同的参数调用并得到相同结果，提示并阻断（`REPEATED_CALL`）。
- 对于副作用工具（`write`、`edit`、`bash`），不再使用全局不变的 `approved_calls` 锁死整轮：允许在文件状态变化后再次编辑同一文件，也允许在修改代码前后多次运行相同的测试命令（如 `pytest` / `unittest`），仅拦截**紧邻上一步、中间没有任何其他工具调用且参数完全相同**的无意义重复写操作。
