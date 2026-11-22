# Codex 项目设置

本仓库把可复用的通用开发方法交给 Codex 插件，把 MiniCC 专属的授权、验收和文档规则留在根目录 `AGENTS.md` 与项目文档中。

## 配置分工

- `AGENTS.md`：项目级常驻约束与文档入口。保持精简，只写通用插件无法知道的 MiniCC 规则。
- `.codex/config.toml`：项目级 Codex 配置。当前将 `superpowers@openai-curated` 设为本项目启用。
- `.codex/rules/*.rules`：Codex 原生的沙箱外命令许可规则。它有自己的命令策略语法，不用于承载代码规范或文档约定。
- `docs/rules/`：MiniCC 的自然语言规则库。根 `AGENTS.md` 自动进入上下文，并要求按任务读取规则索引和相关主题文件。
- `docs/decisions/`：项目决定的依据、取舍、证据和重议条件。
- `.codex/hooks.json` 与 `.codex/scripts/`：为已确认的确定性流程添加 Codex 生命周期自动化。目前只保留扩展位置，没有启用 Hook。

## 外部 Skills

Superpowers 提供通用的软件开发工作流，包括方案规划、测试驱动开发、调试、并行协作和代码审查。本项目将它作为个人 Codex 插件使用，不把插件代码复制到仓库。插件按用户账户安装；其他使用者需要在自己的 Codex 环境中安装，并信任本仓库后加载项目设置。

Superpowers 是强流程插件，会主动引导规划、测试驱动和分阶段审查。MiniCC 自己的授权规则、测试范围选择和 Git 操作权限优先；新会话中如果插件造成多余步骤，再通过本项目设置停用或替换。

在支持该插件市场的 Codex CLI 中，可用下面的命令安装：

```sh
codex plugin add superpowers@openai-curated
```

插件是否已安装可用 `codex plugin list --json --available --marketplace openai-curated` 核对。它不取代 `AGENTS.md` 中的 MiniCC 授权边界、spec 编写条件、决策记录归属和提交/推送授权。

## 自动化边界

Codex Hooks 适合在会话、工具调用或一轮工作结束时启动确定性脚本。开始自动运行测试、格式检查或文档校验前，先确认具体触发条件、耗时和失败如何反馈；当前配置不自动运行检查。定时 Automations 适合按时间重复启动任务，不用来替代一次开发任务里的步骤编排。
