# Agent Note · 模块文档归属

状态：implemented

## 背景与要保留的信息

模块设计、实际代码状态、Review 过程和用户确认的要求容易被堆进同一份文档，随后发生重复或过期。用户明确要求将当前 spec、模块技术说明和重要过程决策分开维护，并以已完成的 Tool 模块作为整理样例。

## 决定与理由

- `docs/specs/<模块名>.md` 是当前有效、用户确认的行为要求和验收条件；它不能根据已写代码反推。
- `docs/modules/<模块名>.md` 是当前真实模块的结构、设计逻辑、实现路径、边界、运行方式和验证证据；根据代码和实际检查持续更新。
- Agent Notes 记录未来可能改变决策的关键取舍、问题根因、Review 发现及修复结论。内容精简并链接到模块文档/spec，不重复实现细节或复述聊天时间线。
- 同一模块采用扁平文档路径并更新已有权威文档；旧草稿仅在信息未迁入、仍有历史价值时保留。
- 使用项目文档 Skill 检查来源、分辨 spec 与实现事实、迁移旧记录并核验链接，避免再次引入相互矛盾的副本。

## 证据与后续

- 现行规则：[AGENTS.md](../../../../AGENTS.md)、[项目架构](../../../../docs/architecture.md)、[开发流程规范](../../../../docs/workflow/development-process-spec.md)
- 整理流程：[project-docs Skill](../../../../.agents/skills/project-docs/SKILL.md)
- Tool 示例：[当前 spec](../../../../docs/specs/tools.md)、[模块实现说明](../../../../docs/modules/tools.md)、[Tool 决策与 Review Note](2026-11-22-tool-module-decisions.md)
