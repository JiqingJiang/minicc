# MiniCC · AI Coding 工作区

第一次使用，告诉 Agent「MiniCC 最终要做到……」。Agent 会把它写进[项目目标](docs/project-goal.md)，每轮开工时说明这次工作和最终目标的关系。

| 要找什么 | 去哪里 |
| --- | --- |
| 每次生效的协作规则 | [AGENTS.md](AGENTS.md)，Claude Code 共用同一份规则 |
| 深入讨论、确认 spec、启动实现 | [开发流程](.agents/skills/develop-change/SKILL.md) |
| 亲自体验、功能测试、深度评测、版本交付 | [验收流程](.agents/skills/review-change/SKILL.md) |
| 需求想法、参考方案和决策理由 | [Agent Notes](.agents/notes/README.md) |
| 模块 spec、当前设计与运行方式 | [模块文档](docs/modules/README.md) |
| Sol 与 Luna 分工 | [模型分工](.agents/skills/model-routing.md) |

## 一轮迭代怎么走

1. **给目标，深聊方案。** 你可以只给最终目标。Agent 查现状、提出可验证的切片、比较方案，并记录值得保留的想法与取舍。
2. **你决定何时写 spec、何时开工。** 可以说「把本轮方案写成 spec」，准备好后说「开始实现这一版」。这些授权随你给出的范围生效，不依赖固定口令。
3. **你先体验。** Agent 给你运行步骤和体验重点，收到体验结果后，先测功能，再做深度评测。评测发现的问题进入下一轮讨论。
4. **你决定何时保存版本。** 一版验收后 Agent 提醒你。明确说「提交并推送这一版」才执行两项操作。只授权提交时，仅产生本地提交。

目录组织参考 DeepSeek Harness 的规则、流程和决策记录设计。[仓库规则](https://github.com/deepseek-ai/deepseek-harness/blob/master/AGENTS.md) · [Agent Notes](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/README.md)
