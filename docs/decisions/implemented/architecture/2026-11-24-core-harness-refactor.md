# 决策记录 · MiniCC 核心 Harness 全量重构（12 板块统一架构）

状态：implemented

## 背景

此前 MiniCC 按单模块逐步累加了 Agent Loop、工作区工具、Memory、Context 和 Skills，但在深度 Code Review 与真实对话测试中暴露出若干架构割裂与边界问题：
1. 缺少只读探索规划（`Plan` 模式）、自主多轮推进（`Goal` 模式）、结构化澄清提问（`ask_user` HITL）、子任务上下文隔离（`SubAgent`）、外部协议扩展（`MCP`）以及真正的命令级 OS 沙箱（`sandbox-exec`）。
2. 会话虽然保存了已完成轮次的 `session.json`，但活动轮次在等待 HITL 审批/澄清/重试或中途退出时没有持久化断点（`checkpoint.json`），重启后无法原地续跑。
3. 此前代码中存在 `approved_calls` 锁死整轮导致无法在改代码前后运行同一条测试命令、`SkillRegistry` 缺失 `state_root` 导入、`install_skill` 未接入审批、`Memory` 跨类别纠错取代死锁以及 `memory`/`skills` 检索分词代码重复等问题。

## 决定与理由

1. **统一原子持久化底座与 `checkpoint.json` 断点续跑**：
   - 抽出 `src/adapters/storage/json_file.py` 统一封装 `fcntl.flock` + `tempfile.mkstemp` + `os.replace`，供 `MemoryStore`、`SkillStateStore`、`TaskStateStore`、`ChatSession` 共用。
   - 在 `ChatSession` 中引入 `checkpoint.json`：每当一轮对话进入 HITL 暂停（`ApprovalRequest`、`ClarificationRequest`、`RetryRequest`）或处于 Goal 模式轮间时，原子落盘当前 `TurnRecord.messages` 与挂起事件载荷。重启后调用 `session.resume()` 可直接结算挂起事件并接着已完成工具结果继续当前轮次，无需重放已有副作用。
2. **模式驱动（`normal` / `plan` / `goal`）而非多套 Loop**：
   - 保持单一 `run_turn()` 协程内核，通过模式策略控制工具可见性与轮次推进：
     - `plan` 模式在工具边界强制只暴露 `read_only=True` 工具、`update_plan` 和 `ask_user`，从根本上杜绝规划阶段误改文件；提交计划时通过 `ApprovalRequest(kind="plan")` 交用户确认。
     - `goal` 模式在 `ChatSession` 层围绕 `TaskState`（`goal`、`plan_steps`、`verification_command`）自动串联多轮 `run_turn()`，并在每步自动运行验证命令或检查未完成步骤，遇到 HITL 暂停时交回控制权。
3. **macOS `sandbox-exec` 进程沙箱 + 强制提权审批**：
   - 在 `src/adapters/sandbox/executor.py` 实现 `SandboxExecutor`。macOS 下默认使用 `sandbox-exec` 生成 Seatbelt Profile，将文件写入严格限制在当前工作区、`~/.minicc` 和临时目录（`/tmp`、`/private/var/folders`），默认关闭网络。
   - `bash` 工具支持 `allow_unsandboxed: bool = False`。一旦请求 `allow_unsandboxed=True`，生成的 `ApprovalRequest(elevated=True)` 即使在 `Auto` 免审模式下也强制要求人工确认，防止模型在 Auto 模式下静默逃逸沙箱。
4. **轻量标准库 MCP 客户端与上下文隔离 SubAgent**：
   - `src/adapters/mcp/client.py` 使用标准库 `subprocess` + JSON-RPC 2.0 over `stdio` 对接 MCP Server，零外部重型依赖，将远端工具映射为 `McpTool(BaseTool)` 纳入统一 `ToolExecutor` 校验与审批。
   - `src/harness/orchestration/subagents/runner.py` 实现 `delegate_subagent`，提供 `explorer`、`planner`、`coder`、`verifier` 四种角色，使用独立 `TurnRecord` 且禁止递归调用，避免子任务检索撑爆主会话 Token。
5. **抽取公共检索层与修复已发现 Bug**：
   - `src/harness/retrieval/lexical.py` 统一 `jieba`（静默日志）、BM25 与余弦相似度。
   - 修复 `approved_calls` 误杀同一测试命令、`Memory` 跨类别取代（允许 `user_correction` 取代 `user_preference`）、`install_skill` 审批、`Workspace.create` 自动建子目录及 `load_skill` 截断问题。

## 证据与后续

- 模块 Spec：[agent-loop.md](../../../specs/agent-loop.md)、[memory-context.md](../../../specs/memory-context.md)、[tools.md](../../../specs/tools.md)
- 模块实现说明：[agent-loop.md](../../../modules/agent-loop.md)、[memory-context.md](../../../modules/memory-context.md)、[skills.md](../../../modules/skills.md)、[tools.md](../../../modules/tools.md)、[subagents.md](../../../modules/subagents.md)
- 重议条件：后续若需接入 HTTP/SSE 远端 MCP Server 或 Linux Landlock / Bubblewrap 原生沙箱，在 `src/adapters/mcp/` 与 `src/adapters/sandbox/` 下扩展对应传输与平台后端即可，无需改动 `harness` 核心层。
