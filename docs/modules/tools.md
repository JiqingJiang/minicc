# 工具、工作区权限、OS 沙箱与 MCP 模块

本模块将模型的工具调用转化为经 Schema 校验、受工作区与 OS 沙箱约束、接入 HITL 审批的本地或 MCP 操作。

当前行为要求见 [工具、沙箱安全与 MCP Spec](../specs/tools.md)。

## 模块结构与代码位置

| 路径 | 职责 |
| --- | --- |
| [`src/harness/tools/base.py`](../../src/harness/tools/base.py) | `BaseTool`（含 `requires_approval`、`read_only` 属性）、`ToolCall`、`ToolResult`、`ApprovalAction` 契约。 |
| [`src/harness/tools/registry.py`](../../src/harness/tools/registry.py) | 按稳定顺序注册与查找工具，支持按运行模式（如 `plan` 只读模式）筛选工具集。 |
| [`src/harness/tools/executor.py`](../../src/harness/tools/executor.py) | Schema Draft 2020-12 校验、严格 JSON 解析、Schema 字节预算过滤、审批暂停与恢复、结果截断。 |
| [`src/harness/permission/workspace.py`](../../src/harness/permission/workspace.py) | 目录 fd + `O_NOFOLLOW` 路径解析、敏感路径拦截、安全递归创建子目录、平台原子目录项交换（`RENAME_SWAP` / `RENAME_EXCHANGE`）。 |
| [`src/adapters/sandbox/executor.py`](../../src/adapters/sandbox/executor.py) | `SandboxPolicy` 与 `SandboxExecutor`：macOS `sandbox-exec` Seatbelt Profile 生成与隔离执行、提权标记与进程组超时清理。 |
| [`src/harness/tools/builtins/`](../../src/harness/tools/builtins/) | `read`、`write`、`edit`、`bash`、`glob`、`grep`、`tool_search` 内置工具实现。 |
| [`src/adapters/mcp/client.py`](../../src/adapters/mcp/client.py) | 基于 `subprocess` + JSON-RPC 2.0 over `stdio` 的轻量级 MCP 客户端与 `McpTool(BaseTool)` 适配器。 |

## 核心设计逻辑

### 1. 工作区文件安全边界（`Workspace`）

- 所有文件操作通过 `os.open(..., dir_fd=fd, O_NOFOLLOW)` 逐级进入子目录，杜绝符号链接跳出工作区。
- `Workspace.create()` 支持在工作区内安全地逐级创建尚不存在的父目录（每级均校验非符号链接、非受保护目录名，并以 `mkdir(..., dir_fd=fd)` 创建），使 `write` 工具可以直接创建如 `src/new_pkg/module.py` 的新文件。
- `Workspace.replace()` 在覆盖已有文件时，先写入同目录随机临时文件并 `fsync`，再调用 macOS `renameatx_np(RENAME_SWAP)` 或 Linux `renameat2(RENAME_EXCHANGE)` 原子交换，随后立即校验被换出的原文件 `(dev, ino)` 与 SHA-256 是否与审批预览基线一致；若不一致则原子回滚或保留备份文件。
- `Workspace.files()` 与 `glob` / `grep` 在达到扫描上限（10,000 个条目或 500 条结果）时，会在返回结果尾部附加明确截断提示，防止模型把“达到扫描上限”误判为“文件不存在”。

### 2. 命令执行与 macOS `sandbox-exec` 沙箱（`SandboxExecutor` & `BashTool`）

- `BashTool` 支持参数 `command: str` 与可选的 `allow_unsandboxed: bool = False`。
- 当 `allow_unsandboxed=False`（默认）且运行在 macOS（存在 `/usr/bin/sandbox-exec`）时，`SandboxExecutor` 动态生成 Seatbelt Profile：
  - `(allow default)` 配合 `(deny file-write*)` 与默认 `(deny network*)`（可按配置开启网络）；
  - 仅显式放行 `(allow file-write* (subpath "<workspace>") (subpath "<state_root>") (subpath "/tmp") (subpath "/private/tmp") (subpath "/private/var/folders") (literal "/dev/null"))`。
  - 任何试图修改工作区、`~/.minicc` 或临时目录之外文件的命令（例如修改 `~/.zshrc` 或桌面其他项目）会被内核直接以 `Operation not permitted` 拦截。
- 当模型发现某项操作被沙箱拦截（或需要联网安装依赖）而请求 `allow_unsandboxed=True` 时，`ApprovalAction.elevated` 为 `True`。**即使当前会话处于 `Auto` 免审批模式，Agent Loop 也会强制暂停并弹出高亮的沙箱外提权审批框**，必须由用户单次显式确认后才会在非沙箱进程组中执行。

### 3. 轻量级 MCP 接入（`src/adapters/mcp/client.py`）

- 采用标准库 `subprocess.Popen` 启动配置的 MCP Server 进程，通过 `stdin`/`stdout` 读写换行分隔的 JSON-RPC 2.0 消息。
- 初始化流程：发送 `initialize` 请求 -> 校验响应 -> 发送 `notifications/initialized` -> 调用 `tools/list` 获取工具列表。
- 每个 MCP 工具包装为 `McpTool(BaseTool)`，名称为 `mcp__<server>__<tool_name>`，默认 `requires_approval = not server_config.get("read_only", False)`，与内置工具共享同一套 `ToolExecutor` 参数校验、超时检查点和 HITL 审批流。

### 4. 工具与 MCP 的持久化启停控制（`/tools` 与 `/mcp`）

- `ToolRegistry` 支持绑定 `state_path`（默认 `~/.minicc/tools.json`），持久化保存用户停用的工具集合。`/tools` 命令以 `1..N` 编号列出所有已注册工具，用户输入编号即可一键切换启用/停用状态；停用后的工具不会从 `registry.all()` 返回。
- `McpManager` 支持绑定 `state_path`（默认 `~/.minicc/mcp.json`），持久化保存每个 MCP Server 的启用/停用状态，并动态同步到 `ToolRegistry`。`/mcp` 命令以 `1..N` 编号展示各 MCP Server 及其工具列表，用户输入编号即可一键开启或关闭对应 MCP Server。
