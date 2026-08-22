# Agent Note: Vibe Coding 开发流程

Status: proposed

## Problem

项目通过 Vibe Coding 方式持续迭代，重要决策和验证结果不能只依赖聊天记录，否则后续实现会失去依据并重复讨论。

## Proposal

采用 Agent Note 记录非机械性变更，并用 `proposed/`、`implemented/`、`rejected/` 和 `archived/` 管理 Note 生命周期。Note 按 `feature`、`bug-fix`、`simplification`、`architecture`、`process` 和 `testing` 分类。

## Alternatives considered

- 保存完整聊天记录：不采用，因为无法作为稳定的设计依据。
- 维护单一项目日志：不采用，因为当前决策和历史决策会混在一起。
- 使用 `achieved/` 表示完成：不采用，因为本项目采用 DeepSeek Harness 的 `implemented/` 表示已交付，`archived/` 表示历史归档。

## Acceptance criteria

- [ ] 非机械性变更在同一个 PR 中新增或更新 Note。
- [ ] Note 路径包含生命周期和分类。
- [ ] `proposed/`、`implemented/`、`rejected/` 和 `archived/` 使用各自的正文结构。
- [ ] Note 包含真实替代方案和验证结果。

## Risks

- 对机械性修改强制写 Note 会增加无效维护成本，因此机械性修改可以豁免。
- 实现完成后如果不迁移 Note 或补充验证，记录会与实际代码脱节。
