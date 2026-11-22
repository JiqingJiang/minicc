# MiniCC

MiniCC 是通用 Agent Harness。现阶段用学校和个人项目中的真实任务验证交付质量、用户介入次数和总 Token。详见[项目目标](docs/goal.md)与[架构和代码目录](docs/architecture.md)。

安装 [Python 依赖](requirements.txt) 并配置 `DEEPSEEK_API_KEY` 后，通过 `python3 -B -m src.app.chat` 开始多轮对话。模型在 [models.json](models.json) 中注册，选用方式见 [Agent Loop 运行说明](docs/modules/agent-loop.md)。工作区工具和审批体验见 [工具系统说明](docs/modules/tools.md)。

## 在这个仓库协作

本仓库使用 Codex 协作。[AGENTS.md](AGENTS.md) 是自动读取的项目入口，并指向[分类规则](docs/rules/README.md)；通用开发工作流使用个人 Codex 安装的 Superpowers Skills。项目流程和取舍见[开发流程规范](docs/workflow/development-process-spec.md)与[项目决策记录](docs/decisions/README.md)。

你可以先给出要解决的问题，与 Agent 一起确定方案和验收方式。准备好后，明确告诉 Agent 要写 spec、开始实现，或两项都做。产品实现后先由你体验，Agent 再做功能测试和评测。验收后由你决定是否提交或推送。

- [Codex 项目设置与外部插件](docs/workflow/codex-setup.md)
- [查开发与验收流程](docs/workflow/development-process-spec.md)
- [查分类项目规则](docs/rules/README.md)
- [查模块实现与当前 spec](docs/architecture.md#模块文档)
- [查项目决策与取舍](docs/decisions/README.md)
