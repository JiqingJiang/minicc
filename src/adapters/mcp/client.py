"""Lightweight JSON-RPC 2.0 over stdio MCP client and BaseTool adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import selectors
import subprocess
from threading import Lock
from time import monotonic

from src.adapters.storage.json_file import atomic_write_json, file_lock
from src.harness.memory.store import state_root
from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure, ToolTimeout
from src.harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    read_only: bool = False
    timeout: float = 30.0

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "McpServerConfig":
        if not isinstance(raw, dict):
            raise ValueError(f"mcp_servers.{name} 配置必须是对象。")
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"mcp_servers.{name}.command 必须是非空字符串。")
        args = raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError(f"mcp_servers.{name}.args 必须是字符串列表。")
        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError(f"mcp_servers.{name}.env 必须是字符串键值对。")
        cwd = raw.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            raise ValueError(f"mcp_servers.{name}.cwd 必须是非空字符串。")
        read_only = bool(raw.get("read_only", False))
        timeout = float(raw.get("timeout", 30.0))
        return cls(
            name=name,
            command=command.strip(),
            args=tuple(args),
            env=dict(env),
            cwd=cwd,
            read_only=read_only,
            timeout=max(1.0, min(300.0, timeout)),
        )


class McpStdioClient:
    """Manage a single stdio MCP server process and exchange JSON-RPC 2.0 messages."""

    def __init__(self, config: McpServerConfig, workspace: Path | None = None) -> None:
        self.config = config
        self.workspace = workspace
        self._process: subprocess.Popen | None = None
        self._next_id = 1
        self._lock = Lock()
        self._initialized = False

    def _ensure_started(self) -> subprocess.Popen:
        if self._process is not None and self._process.poll() is None and self._initialized:
            return self._process
        self.close()
        env = os.environ.copy()
        env.update(self.config.env)
        cwd = Path(self.config.cwd).expanduser().resolve() if self.config.cwd else self.workspace
        try:
            self._process = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except OSError as error:
            raise RuntimeError(f"无法启动 MCP Server {self.config.name!r}: {error}") from error

        self._request_unlocked(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "MiniCC", "version": "1.0.0"},
            },
            timeout=self.config.timeout,
        )
        self._notify_unlocked("notifications/initialized", {})
        self._initialized = True
        return self._process

    def _notify_unlocked(self, method: str, params: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP Server 未启动。")
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False) + "\n"
        self._process.stdin.write(payload.encode("utf-8"))
        self._process.stdin.flush()

    def _request_unlocked(self, method: str, params: dict, *, timeout: float) -> dict:
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("MCP Server 未启动。")
        request_id = self._next_id
        self._next_id += 1
        payload = (
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                ensure_ascii=False,
            )
            + "\n"
        )
        self._process.stdin.write(payload.encode("utf-8"))
        self._process.stdin.flush()

        selector = selectors.DefaultSelector()
        selector.register(self._process.stdout, selectors.EVENT_READ)
        buffer = bytearray()
        deadline = monotonic() + timeout
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"MCP Server {self.config.name!r} 请求 {method} 超时。")
                events = selector.select(min(0.1, remaining))
                if not events:
                    if self._process.poll() is not None:
                        raise RuntimeError(f"MCP Server {self.config.name!r} 已意外退出。")
                    continue
                for key, _ in events:
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        raise RuntimeError(f"MCP Server {self.config.name!r} 关闭了输出流。")
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        raw_line, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(message, dict) or message.get("id") != request_id:
                            continue
                        if "error" in message and message["error"] is not None:
                            err = message["error"]
                            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                            raise RuntimeError(f"MCP 错误: {msg}")
                        result = message.get("result")
                        return result if isinstance(result, dict) else {"value": result}
        finally:
            selector.close()

    def list_tools(self) -> list[dict]:
        with self._lock:
            self._ensure_started()
            result = self._request_unlocked("tools/list", {}, timeout=self.config.timeout)
            tools = result.get("tools", [])
            return [item for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict, *, timeout: float | None = None) -> str:
        with self._lock:
            self._ensure_started()
            result = self._request_unlocked(
                "tools/call",
                {"name": name, "arguments": arguments},
                timeout=timeout or self.config.timeout,
            )
            is_error = bool(result.get("isError", False))
            content_items = result.get("content", [])
            texts: list[str] = []
            if isinstance(content_items, list):
                for item in content_items:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        texts.append(item["text"])
                    else:
                        texts.append(json.dumps(item, ensure_ascii=False))
            elif isinstance(result.get("value"), str):
                texts.append(result["value"])
            else:
                texts.append(json.dumps(result, ensure_ascii=False))
            combined = "\n".join(texts).strip()
            if is_error:
                raise ToolFailure("MCP_TOOL_ERROR", combined or "MCP 工具返回了错误状态。")
            return combined or "MCP 工具执行完成（无输出）。"

    def close(self) -> None:
        proc = self._process
        self._process = None
        self._initialized = False
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass


@dataclass(frozen=True)
class _McpApprovalAction:
    client: McpStdioClient
    remote_name: str
    arguments: dict
    preview: str
    elevated: bool = False
    kind: str = "mcp"

    def execute(self, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            return self.client.call_tool(self.remote_name, self.arguments)
        except ToolFailure:
            raise
        except TimeoutError as error:
            raise ToolTimeout("TOOL_TIMEOUT", str(error)) from error
        except Exception as error:
            raise ToolFailure("MCP_ERROR", str(error)) from error


class McpTool(BaseTool):
    """Wrap a remote MCP tool as a MiniCC BaseTool."""

    def __init__(
        self,
        client: McpStdioClient,
        server_name: str,
        remote_name: str,
        description: str,
        input_schema: dict | None,
        *,
        read_only: bool = False,
    ) -> None:
        safe_server = _SAFE_NAME.sub("_", server_name).strip("_") or "server"
        safe_tool = _SAFE_NAME.sub("_", remote_name).strip("_") or "tool"
        self.name = f"mcp__{safe_server}__{safe_tool}"[:64]
        self.remote_name = remote_name
        self.server_name = server_name
        self.client = client
        self.description = (description or f"MCP tool {remote_name} from {server_name}").strip()
        schema = dict(input_schema) if isinstance(input_schema, dict) else {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        self.parameters = schema
        self.read_only = read_only
        self.requires_approval = not read_only

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction | None:
        if not self.requires_approval:
            return None
        checkpoint()
        preview = (
            f"MCP Server：{self.server_name}\n"
            f"远端工具：{self.remote_name}\n"
            f"参数：\n{json.dumps(arguments, ensure_ascii=False, indent=2)}"
        )
        return _McpApprovalAction(self.client, self.remote_name, arguments, preview)

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            return self.client.call_tool(self.remote_name, arguments)
        except ToolFailure:
            raise
        except TimeoutError as error:
            raise ToolTimeout("TOOL_TIMEOUT", str(error)) from error
        except Exception as error:
            raise ToolFailure("MCP_ERROR", str(error)) from error


class McpStateStore:
    """Persist disabled MCP servers in ~/.minicc/mcp.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def disabled_servers(self) -> set[str]:
        if self.path is None:
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return set()
        if not isinstance(raw, dict):
            return set()
        values = raw.get("disabled_servers", [])
        return {item for item in values if isinstance(item, str)} if isinstance(values, list) else set()

    def set_disabled_servers(self, disabled: set[str]) -> None:
        if self.path is None:
            return
        with file_lock(self.path):
            atomic_write_json(self.path, {"disabled_servers": sorted(disabled)})


class McpManager:
    """Discover, register, and toggle tools from configured MCP servers."""

    def __init__(
        self,
        servers: dict[str, McpServerConfig] | None = None,
        workspace: Path | None = None,
        *,
        state_path: Path | None = None,
    ) -> None:
        self.servers = servers or {}
        self.workspace = workspace
        self._state = McpStateStore(state_path)
        self._disabled_servers: set[str] = self._state.disabled_servers()
        self.clients: dict[str, McpStdioClient] = {
            name: McpStdioClient(cfg, workspace=workspace) for name, cfg in self.servers.items()
        }
        self._server_tools: dict[str, list[McpTool]] = {name: [] for name in self.servers}
        self._registry: ToolRegistry | None = None

    @classmethod
    def from_config(
        cls,
        raw: object,
        workspace: Path | None = None,
        *,
        state_path: Path | None = None,
    ) -> "McpManager":
        if raw is None:
            return cls(workspace=workspace, state_path=state_path)
        if not isinstance(raw, dict):
            raise ValueError("mcp_servers 配置必须是对象。")
        servers = {name: McpServerConfig.from_dict(name, cfg) for name, cfg in raw.items()}
        return cls(servers, workspace=workspace, state_path=state_path)

    def is_server_enabled(self, server_name: str) -> bool:
        return server_name in self.servers and server_name not in self._disabled_servers

    def set_server_enabled(self, server_name: str, enabled: bool) -> bool:
        if server_name not in self.servers:
            return False
        if enabled:
            self._disabled_servers.discard(server_name)
        else:
            self._disabled_servers.add(server_name)
        self._state.set_disabled_servers(self._disabled_servers)
        self._sync_server(server_name)
        return True

    def toggle_server(self, server_name: str) -> bool | None:
        if server_name not in self.servers:
            return None
        new_state = not self.is_server_enabled(server_name)
        self.set_server_enabled(server_name, new_state)
        return new_state

    def server_statuses(self) -> list[dict]:
        """Return status summary for all configured MCP servers and their tools."""
        rows: list[dict] = []
        for name, cfg in self.servers.items():
            enabled = self.is_server_enabled(name)
            tools = self._server_tools.get(name, [])
            rows.append(
                {
                    "name": name,
                    "command": f"{cfg.command} {' '.join(cfg.args)}".strip(),
                    "read_only": cfg.read_only,
                    "enabled": enabled,
                    "tools": [
                        {
                            "name": t.name,
                            "remote_name": t.remote_name,
                            "description": t.description,
                            "enabled": (
                                self._registry.is_enabled(t.name)
                                if self._registry is not None and enabled
                                else enabled
                            ),
                        }
                        for t in tools
                    ],
                }
            )
        return rows

    def _discover_server_tools(self, server_name: str) -> list[McpTool]:
        if self._server_tools.get(server_name):
            return self._server_tools[server_name]
        client = self.clients[server_name]
        discovered: list[McpTool] = []
        try:
            remote_tools = client.list_tools()
        except Exception as error:
            logger.warning("Failed to load MCP tools from %s: %s", server_name, error)
            return []
        for item in remote_tools:
            remote_name = item.get("name")
            if not isinstance(remote_name, str) or not remote_name.strip():
                continue
            tool = McpTool(
                client,
                server_name,
                remote_name.strip(),
                str(item.get("description") or f"MCP tool {remote_name}"),
                item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else None,
                read_only=client.config.read_only or bool(item.get("readOnlyHint", False)),
            )
            discovered.append(tool)
        self._server_tools[server_name] = discovered
        return discovered

    def _sync_server(self, server_name: str) -> None:
        if self._registry is None:
            return
        if self.is_server_enabled(server_name):
            for tool in self._discover_server_tools(server_name):
                if self._registry.get(tool.name, include_disabled=True) is None:
                    self._registry.register(tool)
                self._registry.set_enabled(tool.name, True)
        else:
            for tool in self._server_tools.get(server_name, ()):
                self._registry.unregister(tool.name)
            self.clients[server_name].close()

    def register_tools(self, registry: ToolRegistry) -> list[str]:
        self._registry = registry
        registered: list[str] = []
        for server_name in self.servers:
            if not self.is_server_enabled(server_name):
                continue
            for tool in self._discover_server_tools(server_name):
                if registry.get(tool.name, include_disabled=True) is None:
                    registry.register(tool)
                    registered.append(tool.name)
        return registered

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
