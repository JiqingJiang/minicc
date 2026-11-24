# MiniCC 工具、沙箱安全（Sandbox）与 MCP 模块 Spec

状态：已确认（`refactor/core-harness` 全量重构）。实现事实见 [工具与沙箱模块说明](../modules/tools.md) 及 [MCP 与沙箱模块说明](../modules/mcp-sandbox.md)，方案取舍见 [工具决策记录](../decisions/implemented/architecture/2026-11-22-tool-module-decisions.md) 与 [架构重构决策记录](../decisions/implemented/architecture/2026-11-24-core-harness-refactor.md)。

## 目标与范围

让 Agent 在受控的工作区边界与操作系统沙箱内完成文件读写、精确修改、搜索、命令执行、任务规划、人工提问、子任务委派以及外部 MCP 工具调用。

## 内置工具清单与契约

| 工具名 | 职责 | 模式可用性 | 审批要求（Default / Auto） |
| --- | --- | --- | --- |
| `read` | 有界读取工作区 UTF-8 文本文件与行范围 | 全部模式 | 否 |
| `glob` | 按工作区相对模式列出匹配文件（超上限时明确提示） | 全部模式 | 否 |
| `grep` | 按带超时保护的正则搜索工作区文本 | 全部模式 | 否 |
| `web_search` | 检索互联网公开网页信息（返回标题、URL、摘要列表，支持免配置搜索引擎与可选 Tavily/Brave API） | 全部模式（含 `--chat-only`） | 否 |
| `web_fetch` | 抓取公开 `http`/`https` 网页并提取可读正文、Markdown 或 JSON（内置 SSRF 私网/环回/元数据地址拦截与分段读取） | 全部模式（含 `--chat-only`） | 否 |
| `tool_search` | 按关键词检索当前注册且有效的工具 | 全部模式 | 否 |
| `write` | 创建（含自动创建工作区内相对父目录）或完整更新文件，原子交换防并发冲突 | `normal` / `goal` | Default 每次；Auto 自动 |
| `edit` | 替换文件内唯一匹配文本，生成 unified diff 预览并原子交换 | `normal` / `goal` | Default 每次；Auto 自动 |
| `bash` | 在沙箱（macOS `sandbox-exec`）或受控进程组中执行命令 | `normal` / `goal` | 沙箱内：Default 每条 / Auto 自动；`sandbox=false` 提权：始终要求人工审批 |
| `update_plan` | 更新当前任务的 Goal、Plan 步骤清单（`pending`/`in_progress`/`completed`）与验证命令 | 全部模式 | `plan` 模式下提交最终计划时触发 Plan 确认；其余更新免审 |
| `ask_user` | 向用户提出带候选选项的澄清问题并等待回答（HITL） | 全部模式 | 暂停等待用户回答 |
| `delegate_subagent` | 委派独立上下文的 SubAgent（`explorer`/`planner`/`coder`/`verifier`） | 全部模式（`plan` 模式仅限只读角色） | 否（子 Agent 内部写操作遵循宿主权限） |
| `memory_remember` | 写入或更新长期记忆 | 全部模式 | 否 |
| `memory_search` | 检索全局与当前项目记忆 | 全部模式 | 否 |
| `search_skills` | 搜索在线 Skill 目录 | 全部模式 | 否 |
| `install_skill` | 下载并原子安装 Skill 到 `~/.minicc/skills/` | `normal` / `goal`（非 `--chat-only`） | Default 每次；Auto 自动 |
| `load_skill` | 读取已安装 Skill 的 `SKILL.md` 正文或包内资源 | 全部模式 | 否 |
| `mcp__<server>__<tool>` | 外部 MCP Server 暴露的工具 | `normal` / `goal`（只读标记工具可在 `plan` 使用） | 默认需审批（只读或配置免审除外） |

## 沙箱安全板块（Sandbox & Workspace Permission）要求

1. **工作区文件边界（`Workspace`）**：
   - 使用目录文件描述符（`dir_fd`）与 `O_NOFOLLOW` 逐级解析相对路径，拒绝符号链接、路径穿越（`..`）、绝对路径及敏感凭证文件（`.env`、`.git`、`.ssh`、`*.pem`、`*.key` 等）。
   - 支持在创建新文件（`write`）时，安全地在工作区内逐级创建尚不存在的父目录（拒绝穿过符号链接或受保护目录名）。
   - 覆盖已有文件时，使用平台原子交换（macOS `renameatx_np(RENAME_SWAP)` / Linux `renameat2(RENAME_EXCHANGE)`）核对基线 `(dev, ino)` 与 SHA-256；若审批期间文件被外部修改，自动原子回滚或保留冲突副本。
2. **命令执行沙箱（`SandboxExecutor`）**：
   - **macOS `sandbox-exec` 隔离**：在 macOS 上且系统存在 `/usr/bin/sandbox-exec` 时，`bash` 工具默认以生成的 Seatbelt SBPL Profile 启动进程：
     - 允许读取系统文件与工作区；
     - **仅允许写入当前工作区目录、`~/.minicc` 状态目录以及 `/tmp`（含 `$TMPDIR`）**，禁止写入用户主目录其他位置或系统目录；
     - 默认禁止外部网络访问（除非配置或调用时显式请求）。
   - **受控提权（`allow_unsandboxed`）**：当命令确需访问网络或写工作区外路径时，模型可请求 `allow_unsandboxed=True`。**任何 `allow_unsandboxed=True` 的调用，即使在 Auto 权限模式下也必须强制暂停并向用户展示高亮提权审批提示**，绝不静默放行。
   - **跨平台回退与进程组管理**：非 macOS 或无 `sandbox-exec` 环境回退至独立进程组（`start_new_session=True`），在预览中明确告知无 OS 级文件写隔离；支持单命令超时（300s）与输出上限（16 KiB）优雅强杀整棵进程树（`SIGTERM -> SIGKILL`）。

## MCP 模块要求

1. **协议与传输**：
   - 在 `src/adapters/mcp/` 实现基于标准库 `subprocess` + JSON-RPC 2.0 over `stdio` 的轻量级 MCP 客户端（无需引入重型第三方异步框架）。
   - 支持从 `models.json` 的 `mcp_servers` 字段读取 Server 配置（`command`、`args`、`env`、`cwd`、`read_only`、`timeout`）。
2. **生命周期与映射**：
   - 支持连接初始化（`initialize` + `notifications/initialized`）、工具发现（`tools/list`）与工具调用（`tools/call`）。
   - 每个远端工具映射为 `McpTool(BaseTool)`，名称规范化为 `mcp__<server>__<tool>`，其 JSON Schema 同样经过 `ToolExecutor` 的安全校验（拒绝外部 `$ref` 与超大 Schema），并走统一的 HITL 审批与超时控制。
   - 单个 MCP Server 启动失败或断开时，记录警告并返回明确的工具错误码 `MCP_ERROR`，不阻塞主程序启动或其他本地工具运行。

## 工具、Skill 与 MCP 的交互式启停管理

1. **`/tools` 交互管理**：
   - 输入 `/tools` 列出当前已注册的全部工具（带编号 `1, 2, 3...`、`[启用/停用]` 状态、只读/审批属性与简介）。
   - 用户可直接输入编号（如 `1`、`2 5` 或 `t 1`）切换单个或多个工具的启用/停用状态，或输入 `all on` / `all off`。
   - 工具启停状态持久化到 `~/.minicc/tools.json`，被停用的工具不进入 `ToolRegistry.all()`，不向模型暴露 Schema 且不可执行。
2. **`/skill` 交互管理**：
   - 输入 `/skill` 按编号列出所有已安装 Skill 及总开关状态，支持按编号直接切换启停（`t`）、查看完整正文（`v`）、直接调用（`u`）或切换总开关（`0`），状态持久化到 `~/.minicc/skills.json`。
3. **`/mcp` 交互管理**：
   - 输入 `/mcp` 按编号列出所有已配置的 MCP Server 及其暴露的远端工具与 `[启用/停用]` 状态。
   - 用户可按编号直接开启或关闭指定 MCP Server（及其下属工具），状态持久化到 `~/.minicc/mcp.json`。

## 验收标准

- 工作区越界、符号链接、敏感文件读写被 `Workspace` 拦截；`write` 能在工作区子目录（如 `a/b/c.txt`）安全创建新文件。
- macOS 下 `bash` 默认在 `sandbox-exec` 下运行，尝试写工作区外（如 `~/Desktop` 其他目录）被 OS 沙箱拒绝；请求 `allow_unsandboxed=True` 时即使在 Auto 模式下也强制弹出审批。
- MCP Client 能通过 `stdio` 与本地 JSON-RPC MCP Server 完成握手、`tools/list` 发现和 `tools/call` 执行，并将结果回填 Agent Loop。
