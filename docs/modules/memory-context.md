# Memory 与 Context 模块

本模块管理跨会话长期记忆（`Memory`）与单次模型请求的上下文装配（`ContextAssembler`），并共享统一的词法/语义混合检索内核。

当前要求见 [Memory、Context 与 Skill Spec](../specs/memory-context.md)，方案取舍见 [存储决策](../decisions/implemented/architecture/2026-11-22-memory-context-storage.md) 与 [架构重构决策记录](../decisions/implemented/architecture/2026-11-24-core-harness-refactor.md)。

## 模块结构与代码位置

| 路径 | 职责 |
| --- | --- |
| [`src/harness/retrieval/lexical.py`](../../src/harness/retrieval/lexical.py) | Memory 与 Skill 共享的 `jieba` 静默中文分词、BM25 打分与余弦相似度计算。 |
| [`src/harness/memory/store.py`](../../src/harness/memory/store.py) | 单命名空间（`global` 或 `project`）JSON 存储、关系状态机、跨类别纠错取代与敏感凭证过滤。 |
| [`src/harness/memory/manager.py`](../../src/harness/memory/manager.py) | 全局/项目范围路由、可选本地向量生成与混合检索入口。 |
| [`src/harness/memory/remember_tool.py`](../../src/harness/memory/remember_tool.py) | `memory_remember` 与 `memory_search` 工具定义及来源可信度标记。 |
| [`src/harness/context/assembler.py`](../../src/harness/context/assembler.py) | `ContextBudget`、`ContextAssembler`、分层选材、完整轮次压缩与 `context-traces.jsonl` 记录。 |
| [`src/harness/context/prompt.py`](../../src/harness/context/prompt.py) | `AgentProfile` 与 `PromptCatalog` 角色系统提示词管理。 |

## 核心设计逻辑

### 1. 统一混合检索内核（`src/harness/retrieval/lexical.py`）

将原先分散在 `memory` 和 `skills` 中的分词、BM25 和余弦相似度合并为单一无状态模块：
- 导入 `jieba` 时自动将其日志级别设为 `logging.ERROR`（`jieba.setLogLevel(logging.ERROR)`），彻底消除首次分词时向终端打印 `Building prefix dict...` 的噪声。
- 统一支持中英文词项提取、停用词过滤、BM25 归一化分数 `bm25 / (bm25 + 1.8)` 以及可选 embedding 余弦分数的 `0.62 * lexical + 0.38 * semantic` 融合。

### 2. Memory 存储与跨类别纠错取代

- **范围隔离与免 ID 交互管理（`/memory`）**：`global` 存放于 `~/.minicc/memories.json`（仅限 `user_preference`、`user_correction`）；`project` 存放于 `~/.minicc/projects/<project_id>/memories.json`。在终端输入 `/memory` 直接汇总展示 `global` 与 `project` 的编号列表（`1..N`），用户无需知道内部 ID，直接输入编号即可选择编辑（`e`）、删除（`d`）、查看历史版本（`h`）或解决冲突（`r`），也支持一步指令如 `e 1 新内容`、`d 1`、`s 关键词`。
- **兼容跨类别取代（修复纠错死锁）**：
  - 定义兼容取代组：`{"user_preference", "user_correction"}` 互为兼容类别，`{"project_fact", "decision", "procedure"}` 互为兼容类别。当用户纠正先前的偏好时，模型使用 `category="user_correction", relation="supersede"` 可合法取代旧的 `user_preference` 记录。
  - **来源识别增强**：`RememberMemoryTool.set_source_ref()` 不仅识别“请记住/更正一下”，还覆盖“你说错了/并不是/改成/实际上是/不要再用/我们决定”等自然语言纠错与确认表达，并在 `relation="supersede"` 且用户当轮存在纠错/指示语义时赋予可信来源标记；若模型在非明确指示轮次尝试覆盖旧记忆，自动引导或降级为 `conflict` 冲突组，避免静默丢失或死锁报错。

### 3. Context 分层组装与合规压缩

每次模型请求前，`ContextAssembler.assemble()` 按以下顺序生成上下文：
1. **System Prompt**：包含当前 `AgentProfile`、当前运行模式（`normal`/`plan`/`goal`）、当前模型别名和已提供工具清单摘要（解决模型不知道自己是谁、有多少工具的问题）。
2. **指令文件（`MINICC.md`）**：按顺序加载 `~/.minicc/MINICC.md` 与 `<workspace>/MINICC.md`（单文件 12,000 字符，总计 24,000 字符）。
3. **当前任务与计划状态（`TaskState`）**：注入 `goal`、带状态的 `plan_steps`（`[pending]`/`[in_progress]`/`[completed]`）、`verification_command`、`requirements`、`decisions`、`progress` 和 `open_questions`。
4. **相关 Skills 候选摘要**：仅注入匹配的 Top-K Skill 的 `name`、`description`、`tags`，提示模型按需调用 `load_skill`。
5. **相关长期记忆（Memory）**：自动召回 Top-K 相关记忆（含冲突组同组记录），显式声明为不可信参考数据。
6. **历史消息裁剪与完整轮次压缩**：
   - 历史轮次中超过 `tool_result_max_chars`（默认 4,000 字符）的工具结果在进入 Context 时稳定裁剪并标注原文保留在会话历史中。
   - 当估算总 Token 超过 `compression_limit`（默认可用输入窗口的 80%）时，按完整用户轮次（保持 `assistant.tool_calls` 与 `tool` 消息配对）移除最早的历史轮次，将其结构化摘录合并到系统上下文的 `## 历史会话压缩摘要` 区块中，保证消息序列严格遵循 `system -> user -> assistant (-> tool -> assistant)*` 规范，不会出现游离的前置 `assistant` 消息。
