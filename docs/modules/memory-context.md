# Memory 与 Context

当前实现包括 Agent 记忆写入、定向搜索、请求前自动召回、范围隔离、冲突/时间关系、用户管理、任务状态和 Context 来源追踪。自动化验证结果及未覆盖范围以本轮交付摘要为准；真实模型体验仍待验收。

当前要求见[Memory 与 Context spec](../specs/memory-context.md)；实现方案理由见[存储决策](../decisions/implemented/architecture/2026-11-22-memory-context-storage.md)。整体边界见[架构说明](../architecture.md)。

## 结构与数据位置

- `src/harness/memory/store.py` 提供 JSON 文件存储；所有读改写操作由旁路锁文件上的系统文件锁串行化，写入通过同目录唯一临时文件原子替换，避免并发更新互相覆盖。`manager.py` 负责范围路由、向量生成及旧记录的按需向量补建；`retrieval.py` 使用 BM25、jieba 搜索模式中文分词和余弦语义分数混合排序。默认通过 FastEmbed 在本地 CPU 运行 `BAAI/bge-small-zh-v1.5`，首次使用时下载模型；可在 `models.json` 的 `memory_embeddings` 中指定 `model` 与可选 `cache_dir`，设为 `false` 可关闭向量检索。向量只在本机生成，模型不可用时退回 BM25。全局记忆在 `$XDG_STATE_HOME/minicc/memories.json`，未设置 `XDG_STATE_HOME` 时使用 `~/.local/state/minicc/memories.json`。项目记忆放入相邻的 `projects/<项目身份>/memories.json`。项目身份由 Git 根目录绝对路径的 SHA-256 得出；非 Git 工作区使用其规范化绝对路径，因此同名目录不会共享数据。
- `src/harness/context/assembler.py` 在每次模型请求前装配当前消息、可用历史、`~/minicc/MINICC.md` 全局指令、当前工作区根目录的 `MINICC.md`、当前任务状态、相关记忆和工具预算信息。先加载全局文件，再加载工作区文件；MiniCC 不自动读取 `AGENTS.md` 或嵌套目录中的 `MINICC.md`。单文件最多 12,000 字符，总计最多 24,000 字符。指令超限会在来源记录中标出。自动召回查询由当前用户请求、任务目标和最近确认的决定构成；出现历史意图时才检索已取代记录。
- `src/runtime/session/chat_session.py` 保存会话原始消息，包括工具调用及结果。项目当前会话位于 `projects/<项目身份>/session.json`；再次启动同一项目会自动续接。输入 `/new` 会将旧原文归档到 `sessions/` 并开始新聊天。Memory、任务状态和原始历史使用独立文件。
- `src/runtime/task/state.py` 保存当前项目任务的目标、已确认要求/决定、进度和未解决事项，位于 `projects/<项目身份>/task.json`。状态由用户通过终端命令维护。
- 单次 Context 来源、估算输入 Token、模型适配器可提供的实际输入/输出 Token、裁剪数量和摘要标记追加到 `context-traces.jsonl`。模型请求失败或达到执行时间上限时也会尝试记录本次 trace。用 `/context` 查看当前项目最近记录。

## Memory 使用

终端命令：

```text
/memory [global|project] list
/memory [global|project] search <词>
/memory [global|project] conflicts
/memory [global|project] resolve <ID>
/memory [global|project] edit <ID> <新内容>
/memory [global|project] history <ID>
/memory [global|project] delete <ID>
```

省略范围时默认项目级。Agent 在正常对话循环内判断长期价值并调用 `memory_remember` 写入，按需调用 `memory_search` 定向检索；不另发提取请求。来源类型由当前用户轮次在运行时设定，模型参数不能声明为用户指示或纠正；存储层还要求取代关系带有可信的当前会话/轮次来源标记。当前可信来源标记由用户文本中的有限短语触发，不能作为通用身份认证机制。全局仅允许用户偏好/纠正，项目事实、决定、流程和参考资料只能写入项目范围。工具拒绝疑似凭证值；精确重复合并来源；补充创建关联记录；明确纠正/确认可取代旧记录并保留历史；时间更新标记旧记录过期；冲突与不确定内容保留为同组候选，检索时共同返回。用户编辑保留版本，resolve 选择一侧，delete 删除该条并清理关联。旧聊天不会自动批量回扫。检索最多先选 5 条，若命中未解决冲突组会额外附上组内候选；使用 jieba 分词的 BM25 与可选本地 FastEmbed 语义分数混合排序。FastEmbed 首次运行需下载模型；设 `memory_embeddings` 为 `false` 或本地向量推理失败时退回 BM25。分词依赖 jieba；缺少依赖会使相关记忆读取/写入失败，而不是静默切换为字符分词。

## 实际对话测试发现

以下问题来自用户提供的终端记录，尚未据此修改持久记忆或代码。

- **用户身份被错误写入且旧记录未被纠正。**用户明确说“蒋继庆”，Agent 两次将姓名写成“蒋庆”。记录显示旧条目 `6f7fefbd` 为 `global/project_fact` 且仍为 active；新增条目 `5eef5448` 为 `global/user_correction`，正文仍写错姓名，来源却是 `agent_extraction`。随后用户再次明确纠正时，Agent 没有调用记忆工具，仍用“庆”称呼用户。Context 提示当前用户要求高于长期记忆，但该优先级只是模型输入文本，当前没有执行层保证模型一定遵守。
- **纠正来源识别漏掉常见表达。**`RememberMemoryTool.set_source_ref()` 只匹配少数固定短语，例如“我纠正一下”“更正一下”“不对，”。该测试中的“你实际上……”“你说错了”没有命中，因此无法为取代操作生成可信的 `user_correction` 来源。测试记录中的新条目正是 `agent_extraction`。
- **错误类别的旧记录无法用关系操作迁移。**Agent 首先以 `project_fact/global` 写入，被范围校验正确拒绝；之后的新纠正被存为 `user_correction/global`。旧条目和新条目类别不同，`supersede` 又要求同类别，因此后续取代尝试失败；改用 project 范围时则找不到 global 记忆 ID。当前没有明确的跨类别纠错/迁移操作。Agent 还把“放宽跨类别限制”列为修复选项；这会改变通用关系约束，不能作为这个个案的静默修复。不要通过直接编辑 `memories.json` 修复：这会绕过存储锁、原子写入及关系维护。
- **个人资料被无确认地合并保存。**同一条自动记忆包含了姓名、出生日期/时间/地点、八字和 MBTI，用户当时是在纠正身份，并未明确要求保存这整组资料。当前敏感值过滤主要针对密码、令牌和密钥，不涵盖此类个人资料；`memory_remember` 按现有设计不要求审批。是否应要求用户明确 opt-in 或逐项确认，属于待定的产品/隐私边界，不应由实现文档自行定为新规则。
- **失败后 Agent 对状态作出不可靠陈述。**工具返回范围、类别和 ID 错误后，Agent 仍反复尝试不兼容的组合，最后声称两条记忆会同时召回且新记录权重更高。排序由词法与语义分数决定，创建时间只作为分数相同时的次级排序键；不能据“更新、更丰富”保证新记忆优先。该回答还建议直接编辑 JSON，与本模块的并发/原子写入边界冲突。
- **记忆范围命令反馈容易误解。**`/memory list` 默认只查 project 范围，所以项目为空时会显示“还没有这类记忆”，即使 global 范围已有记录；`/memory global list` 才会显示全局内容。行为符合当前默认范围，但空结果未明确显示正在查看的范围。
- **首次分词有启动噪声。**jieba 首次加载词典时把 `Building prefix dict` 等初始化信息写到终端；这是初始化日志，不是检索失败，但会夹杂在聊天输出中。

## 任务状态和历史

```text
/task view
/task goal <目标>
/task requirement <已确认要求>
/task decision <已确认决定>
/task progress <进度>
/task open <待解决事项>
/task clear
```

`/new` 也会把当前任务状态归档到 `tasks/`，新聊天从空任务状态开始。归档文件保留，但终端目前没有选择和恢复旧归档的命令。

Context 预算默认为 24,000 个估算 Token；估算对中日韩字符按约 1.5 字符折算，对其他字符按约 4 字符折算，并将实际可用工具 schema 纳入估算。该值只是请求前估算，实际输入/输出 Token 以模型适配器返回值为准。超限时从最早完成的对话轮次开始移除，并在本次请求加入最多 8 条有界片段摘要。大型工具结果在请求视图中保留首尾片段，完整原文留在会话记录中。历史摘要标记为系统摘要并关联被覆盖的会话轮次 ID。当前用户请求、系统提示、任务状态和指令不会因历史裁剪而删除。当前版本每个项目只有一个可直接续接的聊天；旧聊天和任务状态会归档，但还没有选择和恢复旧归档的命令，也没有自动从工具结果更新任务状态。

## 验证与限制

自动化回归入口为 `.venv/bin/python -B -m unittest discover -s tests -v`；`tests/test_memory.py` 覆盖来源伪造防护、可信来源取代和并发写入，其他测试覆盖工具循环与终端审批。裸系统 `python3` 若未安装 `requirements.txt` 中的依赖，会在测试导入时失败。验证不覆盖在线模型质量。用户验收前仍应验证全局与不同项目隔离、记忆新增/编辑/冲突解决/删除及敏感值拒绝、上下文召回与预算裁剪、重启后的历史恢复和 Context trace。

## 错误处理

记忆文件缺失按空存储处理；JSON 损坏、记录格式无效或文件访问失败会抛出存储错误。记忆工具的参数/关系错误会作为可识别工具错误返回，其他存储异常由工具执行器归一化为通用工具失败。FastEmbed 初始化、向量化或补建失败会记告警并退回 BM25；`jieba` 是必需分词依赖，缺失时记忆读写/检索无法正常完成。任务状态文件损坏会阻止本次 Context 组装；不可读的指令文件会跳过。Context trace 写入失败只记日志，不阻断模型请求；会话持久化写入失败同样只记日志，不能据请求成功推断已落盘。当前没有模型生成式摘要；压缩使用历史摘录，保留原文供后续恢复。未实现过期自动判定、多设备同步或自动旧历史回扫。

在线模型质量、首次下载 FastEmbed 模型后的语义效果和完整终端体验仍需人工验收。安装 `requirements.txt` 后，首次启用本地向量检索需要下载 embedding 模型；没有网络或模型加载失败时向量功能不可用，检索退回 BM25。
