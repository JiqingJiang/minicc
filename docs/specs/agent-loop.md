# MiniCC Agent Loop、模式（Normal / Plan / Goal）、HITL 与 SubAgent Spec

状态：已确认（`refactor/core-harness` 全量重构）。实现事实见 [Agent Loop、模式、HITL 与持久化模块说明](../modules/agent-loop.md) 及 [SubAgent 编排模块说明](../modules/subagents.md)，方案取舍见 [架构重构决策记录](../decisions/implemented/architecture/2026-11-24-core-harness-refactor.md)。

## 目标与范围

将用户输入推进为受控的模型请求、工具执行、人工交接（HITL）、多步规划（Plan 模式）、自主迭代（Goal 模式）、子任务委派（SubAgent）以及跨进程可恢复的持久化会话。

1. **Agent Loop**：驱动 `Context 组装 -> 模型调用 -> 工具校验与执行 -> 结果回填` 的核心状态机。
2. **三种运行模式**：
   - `normal`：标准读写与命令执行模式，遵循 `default`（逐项审批）或 `auto`（本会话自动批准工具副作用，不跳过越界提权审批）权限策略。
   - `plan`（参考 Claude Code `/plan`）：只读探索与规划模式。Loop 仅向模型暴露只读工具（`read`、`glob`、`grep`、`tool_search`、`memory_search`、`load_skill`）以及规划/澄清工具（`update_plan`、`ask_user`）。禁止执行 `write`、`edit`、`bash` 或写操作 MCP 工具。模型生成结构化计划（目标、步骤清单、验证命令）后，向用户发起 Plan 审批（`ApprovalRequest(kind="plan")`），用户批准后可一键切入 `normal` 或 `goal` 模式按计划执行。
   - `goal`（参考 `/goal`）：自主目标推进模式。围绕 `TaskState` 中的 `goal`、计划步骤（`plan_steps`）和可选验证命令（`verification_command`）自动推进。当模型完成一轮回复但任务仍有未完成的 `plan_steps` 或验证命令未通过时，Loop 自动注入下一步推进/验证提示继续迭代，直到全部步骤完成且验证通过、遇到 HITL 暂停，或达到 Goal 轮次预算上限。
3. **HITL 人工交接**：
   - `ApprovalRequest`：用于文件修改（`write`/`edit`）、命令执行（`bash`）、在线安装 Skill（`install_skill`）、沙箱外提权（`unsandboxed`）、MCP 工具调用和 Plan 确认。
   - `ClarificationRequest`（由 `ask_user` 工具触发）：当需求存在关键歧义时，模型提供结构化问题与 2–4 个候选选项，暂停 Loop 等待用户选择或输入自定义回答，回答作为工具结果回填继续当前轮次。
   - `RetryRequest`：模型 API 异常或超时时暂停，保留本轮已成功的工具副作用，支持 `/retry` 原地恢复。
4. **持久化与断点续跑（Persistence & Resume）**：
   - 会话历史（`session.json`）、任务与计划状态（`task.json`）及活动轮次断点（`checkpoint.json`）实时通过文件锁原子落盘。
   - 当进程在等待 HITL（审批、澄清、重试）或 Goal 模式轮间中断退出后，重新启动或执行 `/resume` 时，能够从 `checkpoint.json` 恢复挂起的请求、已完成工具结果和待决问题，继续执行而无需重放已完成的副作用。
5. **SubAgent 编排**：
   - 通过 `delegate_subagent` 工具启动隔离上下文的子 Agent。
   - 支持角色：`explorer`（只读代码探索与检索）、`planner`（只读方案设计）、`coder`（在父权限策略下读写修改）、`verifier`（运行测试或核对结果）。
   - 子 Agent 拥有独立的消息列表和精简 Context（只含子任务提示、项目指令和必要工具），不复制父会话完整历史，避免污染父 Agent 上下文窗口；共享宿主 HITL 审批策略与剩余时间预算，完成后向父 Agent 返回结构化摘要。

## 边界与预算约束

- 单轮默认上限：最多 40 次模型请求、80 次工具尝试、900 秒活动执行时间（HITL 等待时间不计入）。
- Goal 模式附加保护：默认连续自动推进最多 8 轮（可配置），若连续轮次无进展或验证持续失败则暂停交回用户。
- SubAgent 预算：单次子任务默认最多 12 次模型请求、24 次工具尝试，递归深度上限为 1（禁止子 Agent 再派发子 Agent）。
- 去重与防死循环：同一轮内已被用户拒绝的相同调用（`name + canonical_args`）不再重复弹窗；只读工具连续 3 次相同调用且结果不变时警告并阻断；允许 `bash` 或文件已发生变更后的 `write`/`edit` 在同一轮内再次执行（支持“跑测试 -> 改代码 -> 再跑同一测试”的标准研发循环）。

## 验收标准

- `normal`、`plan`、`goal` 三种模式可切换；`plan` 模式下写工具不可被调用，产出计划获批后可转入执行。
- `ask_user` 能暂停 Loop 并接收选项编号或自定义文本回填。
- 挂起在 `ApprovalRequest`、`ClarificationRequest` 或 `RetryRequest` 的会话在销毁 `ChatSession` 实例并重新加载后，能通过 `resume()` 恢复并完成该轮对话。
- `delegate_subagent` 能以只读 `explorer` 角色在独立上下文中完成检索并将结论返回父 Agent，且禁止嵌套调用 `delegate_subagent`。
