# SubAgent 编排模块

本模块为 MiniCC 提供上下文隔离的子任务委派能力（参考 Claude Code 的 `Task` / Subagent 机制），让主 Agent 能将高噪的代码搜索、独立方案分析、局部修改或测试验证委派给短生命周期的子 Agent，从而大幅降低主会话的上下文膨胀与总 Token 消耗。

当前行为要求见 [Agent Loop、模式、HITL 与 SubAgent Spec](../specs/agent-loop.md)。

## 模块结构与代码位置

| 路径 | 职责 |
| --- | --- |
| [`src/harness/orchestration/subagents/runner.py`](../../src/harness/orchestration/subagents/runner.py) | `SubAgentRole`、`SubAgentResult`、`SubAgentRunner` 以及暴露给父 Agent 的 `DelegateSubAgentTool`（`delegate_subagent`）。 |

## 核心设计逻辑

1. **四种预设角色与权限收敛**：
   - `explorer`（只读探索）：仅暴露只读工具（`read`、`glob`、`grep`、`tool_search`、`memory_search`、`load_skill`），用于快速定位代码实现、查找调用链并汇总证据。
   - `planner`（只读规划）：仅暴露只读工具，用于分析备选方案并向父 Agent 返回结构化建议。
   - `coder`（局部实现）：暴露读写与命令工具（在父会话处于 `plan` 只读模式时被禁止调用），共享父会话的宿主 `approval_policy`（Default 模式下仍需用户批准，Auto 模式下自动批准沙箱内操作）。
   - `verifier`（测试验证）：暴露只读工具与 `bash`，用于运行测试套件并提取失败摘要。
2. **上下文隔离与防递归**：
   - 子 Agent 启动时创建全新的 `TurnRecord`，**不复制父会话的历史消息**，仅注入：
     - 对应角色的精简 System Prompt；
     - 工作区 `MINICC.md` 指令与当前 `TaskState` 摘要；
     - 父 Agent 传入的 `task`（任务描述）与可选 `context`（已知背景/文件路径）。
   - 过滤掉 `delegate_subagent` 与 `ask_user` 工具，严格限制递归深度为 1（子 Agent 不能再派生孙 Agent，遇到不确定问题直接向父 Agent 报告）。
3. **独立预算与结果回收**：
   - 单次子 Agent 默认上限为 12 次模型请求、24 次工具尝试、180 秒活动时间（且不超过父轮次剩余截止时间）。
   - 执行完毕后，向父 Agent 返回包含 `[角色 / 模型请求数 / 工具调用数 / Token 用量]` 元数据头与最终结论的结构化文本，并将子 Agent 的 Token 用量计入父轮次统计。
