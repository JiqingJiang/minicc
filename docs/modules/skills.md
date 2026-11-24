# Skill 模块

本模块管理 MiniCC 私有 Skill 库（`~/.minicc/skills/`）的本地发现、元数据混合检索、按需正文/资源加载以及受审批保护的在线目录搜索与安装。

当前要求见 [Memory、Context 与 Skill Spec](../specs/memory-context.md) 及 [工具模块 Spec](../specs/tools.md)。

## 模块结构与代码位置

| 路径 | 职责 |
| --- | --- |
| [`src/harness/skills/models.py`](../../src/harness/skills/models.py) | `Skill` 数据类与 `SkillProvider` 协议。 |
| [`src/harness/skills/state.py`](../../src/harness/skills/state.py) | `~/.minicc/skills.json` 全局与单项启停状态的带锁原子持久化。 |
| [`src/harness/skills/registry.py`](../../src/harness/skills/registry.py) | `SKILL.md` 发现、YAML Frontmatter 解析、按需正文/包内资源读取及原子包安装。 |
| [`src/harness/skills/matcher.py`](../../src/harness/skills/matcher.py) | 复用 `lexical.py` 对 Skill 名称、标签和描述做 BM25 + 可选本地向量混合排序。 |
| [`src/harness/skills/marketplace.py`](../../src/harness/skills/marketplace.py) | SkillMD 在线公开目录只读搜索与包拉取、SHA-256 校验及重定向主机限制。 |
| [`src/harness/skills/tool.py`](../../src/harness/skills/tool.py) | `search_skills`、`install_skill`（需 HITL 审批）与 `load_skill` 工具实现。 |

## 核心设计逻辑与修复事实

1. **只注入摘要、按需加载正文**：
   - `ContextAssembler` 仅注入 `SkillRegistry.select()` 返回的 Top-K 元数据（`name`、`description`、`tags`）。
   - 模型确定某项 Skill 适用后，调用 `load_skill(name)` 获取 `SKILL.md` 正文，或调用 `load_skill(name, file_path="references/...")` 读取包内文本资源。
2. **输出截断与 XML 标签完整性**：
   - `LoadSkillTool` 将正文与资源裁剪控制在 `ToolExecutor` 的结果预算内，确保闭合标签 `</skill_content>` 和 `</skill_resource>` 永远不会被外层执行器截断，并附带清晰的分页/子资源读取提示。
3. **在线安装纳入 HITL 审批保护**：
   - `InstallSkillTool` 设置 `requires_approval = True`，在执行下载与本地写入前先通过 `approval()` 生成包含目标 `skill_id` 和安装目录 `~/.minicc/skills/` 的审批预览。Default 模式下必须经用户批准才执行安装，非交互模式默认拒绝，`--chat-only` 模式不注册 `install_skill`。
4. **安全校验**：
   - 修复了 `SkillRegistry` 默认 `state_root()` 导入缺失以及嵌套 YAML Frontmatter 折叠块（`>`）条件表达式优先级括号缺失导致的文本丢失问题。
   - 安装与资源读取均严格拒绝绝对路径、`..` 路径穿越、符号链接以及 `.env`/`*.pem`/`*.key` 等敏感路径。
