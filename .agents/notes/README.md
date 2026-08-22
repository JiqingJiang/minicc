# Agent Notes

Agent Note 记录影响代码库的决策或提案：代码和普通文档无法承载的原因、取舍、后果与验证依据。Agent Note 的状态和类别由路径编码。

## 路径和命名

```text
{lifecycle}/{class}/YYYY-MM-DD-topic-title.md
```

日期是主题首次提出的日期。跨 Note 引用必须使用相对 Markdown 链接，不使用裸文件名、编号或依赖文件历史的描述。

不要创建集中式 `INDEX.md`。当前 Note 清单由生命周期和类别目录构成；使用目录浏览或搜索定位记录。

## 生命周期

### `proposed/`

实施前评审中的提案，尚未实现或仅部分实现。过时的提案移动到 `rejected/`，不要移动到 `archived/`。

### `implemented/`

已经交付的决策。正文描述当前实际存在的路径、名称、结构、行为和限制；后续代码事实发生变化时，在同一变更中同步更新 Note，但不能借此改写原决策。

### `rejected/`

经过讨论后否决的提案。保留它的前提是：其否决原因仍能阻止一种有诱惑力且影响重大的错误；否则删除该 Note。被否决的 Note 冻结，不再修改。

### `archived/`

已经实现，但其决策依据不再具有当前指导价值的历史记录。只有 `implemented/` Note 可以归档。归档后永久冻结，不得编辑、翻译、重排、移动或删除，也不能作为当前行为的权威来源。

本项目没有 `achieved/` 生命周期。已交付使用 `implemented/`，历史归档使用 `archived/`。

## 分类

分类是封闭集合。Note 必须放入下列分类之一；新增分类必须先修改本文件、目录规则和校验规则。

| 分类 | 范围 |
|---|---|
| `feature` | 面向用户或模型的新能力。 |
| `bug-fix` | 修复缺陷，或补齐事故复盘发现的缺口。 |
| `simplification` | 不增加能力的前提下删除代码、行为或对外范围。以可观察行为是否改变作为判别标准。 |
| `architecture` | 交付源码的结构性决策：模块关系、运行时模型、接口边界和扩展点。 |
| `process` | 代码周边的工具、策略和工作流：CI、包管理、文档、Agent 流程和仓库治理。 |
| `testing` | 测试基础设施、测试策略和测试数据。 |

`architecture` 处理交付运行时；`process` 处理围绕代码的工作流和工具。不存在单独的 `refactor` 分类；没有可观察行为变化的重构归入 `simplification`，有行为或结构决策的重构归入对应分类。

## 何时必须创建或更新

非机械性变更必须在同一个 PR 中新增或更新至少一份 Note。以下变更属于非机械性变更：

- 修改用户或模型可见行为。
- 修改架构、模块边界或跨文件约定。
- 修改公共接口、配置格式、持久化格式或协议格式。
- 修改工具链、开发流程或安全策略。
- 修改测试策略或测试基础设施。
- 引入维护者未来可能重新审视的重要取舍。

重大未来工作从 `proposed/` 开始；已经做出的决策从 `implemented/` 开始。更新已有 Note 即可，不要为同一个决策创建重复记录。

以下修改可以豁免：纯格式调整、机械重命名、没有行为/结构/流程/理由变化的局部编辑。

Agent Note 不能被直接改写成另一个决策。新决策应创建新 Note，并与被取代的 Note 互相链接。只有在新 Note 保留旧 Note 全部独有的依据、替代方案、后果、验证和覆盖缺口后，才允许完全合并并删除旧 Note。

## 文件格式

机器检查的头部字段保持英文；正文可以使用中文。

### 头部

前三行必须严格为：

```markdown
# Agent Note: <title>

Status: <status>
```

允许的状态值：

```text
Status: proposed
Status: implemented
Status: rejected — <reason>
```

状态必须与所在生命周期目录一致。日期写在文件名中，不写入状态行。

### `proposed/` 正文

```markdown
## Problem
## Proposal
## Alternatives considered
## Acceptance criteria
## Risks
```

`Proposal` 使用将来时态，描述拟议变更、迁移步骤和未决问题。`Acceptance criteria` 描述可观察的完成状态。`Risks` 同时记录可能出错的地方和有意放弃的能力。

### `implemented/` 正文

```markdown
## Problem
## Decision
## Alternatives considered
## Consequences
```

`Decision` 使用现在时态描述已经交付的现实。`Consequences` 同时记录决策带来的收益和代价。已实现 Note 不使用 `Proposal`、`Plan`、`Migration plan` 或 `Acceptance criteria` 等提案阶段标题；当前验证可使用 `Testing`、`Verification`、`Deferred` 或 `Related`。

### `rejected/` 正文

保留提案阶段的正文结构，否决结论写在状态行：

```markdown
Status: rejected — <一行否决原因>
```

### `archived/` 正文

保留归档前的 `Status: implemented`，紧接状态行增加：

```markdown
Archived: YYYY-MM-DD
```

归档操作只允许移动文件、更新归档元数据、修复入站链接和维护归档清单；不得改写决策正文。

## 必需的替代方案记录

每份 Note 都必须有 `## Alternatives considered`。只记录真实考虑过的方案，并说明每个方案为什么没有被采用；不要为了填格式而虚构替代方案。

## 生命周期迁移

```text
proposed → implemented
proposed → rejected
implemented → archived
```

移动文件时必须在同一个变更中更新 `Status:` 并满足目标生命周期的正文结构：

- `proposed → implemented`：将 `Proposal` 改为现在时态的 `Decision`，把验收条件和风险折入 `Consequences` 或当前验证章节，删除计划语气。
- `proposed → rejected`：只增加带原因的 `Status:`，保留提案正文并冻结。
- `implemented → archived`：保留已实现内容，增加 `Archived:` 日期并冻结。

## 语言规则

本项目以中文 Note 为主。机器检查所需的文件头、状态值和规范章节名保持英文；决策正文、替代方案、后果和验证结果使用中文。
