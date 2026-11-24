# MiniCC Memory、Context 与 Skill 模块 Spec

状态：已确认（`refactor/core-harness` 全量重构）。实现事实见 [Memory 与 Context 模块说明](../modules/memory-context.md) 及 [Skill 模块说明](../modules/skills.md)，方案取舍见 [存储决策](../decisions/implemented/architecture/2026-11-22-memory-context-storage.md) 与 [架构重构决策记录](../decisions/implemented/architecture/2026-11-24-core-harness-refactor.md)。

## 目标与术语

- **Memory**：跨会话持久化的长期偏好、用户纠正、项目事实、已确认决定、流程经验和参考线索。
- **Skill**：按需发现、延迟加载正文与包内资源的任务技能包（`SKILL.md`）。
- **Context**：每次模型请求前，按优先级与 Token 预算从各权威来源选材、裁剪、压缩并生成可审计的模型输入视图。

## Memory 模块要求

1. **分类与范围隔离**：
   - 类别：`user_preference`、`user_correction`、`project_fact`、`decision`、`procedure`、`reference`。
   - 范围：`global`（仅允许 `user_preference` 与 `user_correction`）与 `project`（按 Git 根目录或工作区规范化绝对路径 SHA-256 隔离）。
2. **写入、更新与纠错（解决跨类别取代死锁）**：
   - 提供 `memory_remember` 与 `memory_search` 工具。
   - 关系支持：`new`、`duplicate`、`supplement`、`supersede`、`time_update`、`conflict`、`uncertain`。
   - **纠错与取代规则**：允许同类别取代，且显式允许 `user_correction` 取代 `user_preference`（以及 `decision` 与 `project_fact` 之间的演进取代）。来源类型由当前用户轮次文本或显式纠正意图标记判定；精确重复自动合并来源，冲突组在检索时成对返回。
   - **敏感信息拦截**：拒绝包含 API Key、Token、私钥或密码的文本写入。
3. **混合检索与人性化免 ID 管理**：
   - 统一复用 `src/harness/retrieval/lexical.py` 的 BM25 + `jieba` 搜索分词（静默启动日志）+ 可选本地向量余弦相似度。
   - **免 ID 编号交互菜单（`/memory`）**：用户输入 `/memory` 时，自动汇总并按连续编号（`1, 2, 3...`）列出当前全局（`global`）与项目（`project`）记忆，无需用户记忆或输入 ID；用户可直接输入编号（或快捷指令如 `e 1`、`d 1`、`h 1`、`r 1`、`s <关键词>`）完成修改、删除、查看历史、解决冲突与搜索，同时向后兼容命令行子命令。

## Skill 模块要求

1. **私有库与发现**：
   - 存储于 `~/.minicc/skills/<name>/SKILL.md`，支持 YAML Frontmatter（`name`、`description`、`tags`、`when_to_use`、`disable-model-invocation`、`user-invocable`）。
   - Context 阶段仅注入匹配的 Top-K Skill 摘要（`name`、`description`、`tags`），不整篇载入正文。
2. **按需加载与在线安装安全**：
   - 模型通过 `load_skill(name, file_path?)` 按需读取 `SKILL.md` 正文或包内相对路径资源文件；拒绝路径穿越与敏感后缀。
   - 在线搜索（`search_skills`）与安装（`install_skill`）：`install_skill` **必须标记 `requires_approval = True`**，在 Default 模式下展示将要安装的 Skill ID 与来源并经用户批准后才下载写入本地；`--chat-only` 模式下不暴露 `install_skill`。

## Context 模块要求

1. **输入来源与固定顺序**：
   - Agent Profile System Prompt（含当前运行模式 `normal`/`plan`/`goal`、当前模型别名与工具能力边界说明）；
   - 全局 `~/.minicc/MINICC.md` 与工作区 `./MINICC.md` 指令（单文件上限 12,000 字符，总上限 24,000 字符）；
   - 当前任务状态（`TaskState`：Goal、Plan 步骤及状态、已确认要求/决定、验证命令、待解决事项）；
   - 匹配的 Skill 候选摘要与 Memory 候选记录（均标注为不可信参考数据，不得覆盖当前用户指令）；
   - 会话历史（超预算时对早期完整轮次生成结构化摘录摘要，并对历史超长工具输出按 `tool_result_max_chars` 裁剪，保留原始会话落盘证据）；
   - 当前轮次消息与配对的工具调用/结果。
2. **可追溯性（Context Trace）**：
   - 每次请求记录 `context-traces.jsonl`，包含各来源、估算 Token、实际输入/输出/缓存 Token、裁剪消息数与预算溢出情况。

## 验收标准

- 全局与项目 Memory 严格隔离；`user_correction` 可以正常 `supersede` 旧的 `user_preference`。
- Skill 仅注入摘要到 Context，`load_skill` 按需加载正文与子资源且不会被执行器截断破坏闭合标签；`install_skill` 触发 HITL 审批。
- 历史超预算时按完整轮次压缩，保留 `tool_calls` 与 `tool` 消息配对，且压缩后的消息角色序列合法。
