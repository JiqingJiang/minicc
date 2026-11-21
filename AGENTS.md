# MiniCC 协作规则

- 每次开始工作，先读[项目目标](docs/project-goal.md)，向用户简述本轮要达成什么、怎样推进最终目标、怎样验收。目标由用户提供。首次收到明确目标就记录，后续改变目标须经用户确认。按[上下文加载规则](.agents/context-loading.md)查阅相关模块和历史决策。
- 任何实现前先与用户深入讨论问题、方案、参考依据、取舍和验收。用户明确同意整理为 spec 后才写 spec。仅在用户明确要求开始当前范围的实现后才改代码、测试和配置或委派这些改动。用户可以在同一条消息明确授权多个动作。深入讨论和两个授权点见[开发流程](.agents/skills/develop-change/SKILL.md)。
- 及时把新想法、有意义的讨论与决策写入 [Agent Notes](.agents/notes/README.md)，并跟进到落地、归档或否决。模块的使用方式、实现逻辑、spec 和验证方法写在 [docs/modules](docs/modules/README.md) 对应目录。
- 重要任务优先由 GPT-6.2-Sol 主导。获准实现后，将边界清楚的执行工作按[模型分工](.agents/skills/model-routing.md)交给 Luna，由主 Agent 整合和复核。
- 实现后依次完成用户亲自体验、Agent 功能测试、深度评测。每版验收后提醒用户保存版本。只有用户明确授权才执行 Git 提交或推送。阶段、证据和授权见[验收与版本交付](.agents/skills/review-change/SKILL.md)。
