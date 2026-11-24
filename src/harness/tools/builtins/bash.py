"""Bash command execution with macOS Seatbelt sandbox-exec and elevated unsandboxed approval."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
from time import monotonic

from src.adapters.sandbox.executor import SandboxPolicy, sandbox_available, wrap_sandbox_command
from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure, ToolTimeout

_MAX_COMMAND = 7_000
_MAX_OUTPUT = 16 * 1024
_COMMAND_TIMEOUT = 300


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a Bash command in the workspace. On macOS, commands run inside a Seatbelt sandbox restricting "
        "file writes to the workspace, ~/.minicc, and /tmp. Set allow_unsandboxed=true only when outside "
        "write or network access is required (always requires explicit user approval)."
    )
    requires_approval = True
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1, "maxLength": _MAX_COMMAND},
            "allow_unsandboxed": {"type": "boolean", "default": False},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, cwd: Path, *, allow_network: bool = False, use_sandbox: bool = True) -> None:
        self.cwd = Path(cwd).resolve(strict=True)
        if not self.cwd.is_dir():
            raise ValueError("Bash 工作目录必须是目录。")
        self.policy = SandboxPolicy(workspace=self.cwd, allow_network=allow_network)
        self.use_sandbox = use_sandbox

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        del arguments, checkpoint
        raise ToolFailure("APPROVAL_REQUIRED", "bash 命令需要先经用户批准。")

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction:
        command = arguments["command"]
        unsandboxed = bool(arguments.get("allow_unsandboxed", False)) or not self.use_sandbox
        shell = shutil.which("bash")
        if shell is None:
            raise ToolFailure("BASH_UNAVAILABLE", "系统中找不到 Bash。")
        checkpoint()
        will_sandbox = not unsandboxed and sandbox_available()
        if will_sandbox:
            note = "沙箱保护：已启用 macOS Seatbelt 沙箱（仅允许写入当前工作区、~/.minicc 与 /tmp）。"
        elif unsandboxed and bool(arguments.get("allow_unsandboxed", False)):
            note = "【提权请求】此命令请求在沙箱外（allow_unsandboxed=true）运行，可访问工作区外的文件和网络！"
        else:
            note = "此命令以当前用户权限运行，可访问工作区外的文件和网络；系统没有限制它只能操作工作区。"
        preview = f"工作目录：{self.cwd}\n命令：\n{command}\n\n{note}"
        return BashAction(
            self.cwd,
            shell,
            command,
            preview,
            policy=self.policy,
            unsandboxed=unsandboxed,
            elevated=bool(arguments.get("allow_unsandboxed", False)),
        )


@dataclass(frozen=True)
class BashAction:
    cwd: Path
    shell: str
    command: str
    preview: str
    policy: SandboxPolicy | None = None
    unsandboxed: bool = False
    elevated: bool = False
    kind: str = "bash"

    def execute(self, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        policy = self.policy or SandboxPolicy(workspace=self.cwd)
        argv, _ = wrap_sandbox_command(self.shell, self.command, policy, unsandboxed=self.unsandboxed)
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                start_new_session=True,
            )
        except OSError as error:
            raise ToolFailure("BASH_START_FAILED", "无法启动已批准的 Bash 命令。") from error

        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        truncated = False
        deadline = monotonic() + _COMMAND_TIMEOUT
        try:
            while selector.get_map():
                checkpoint()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ToolTimeout(
                        "TOOL_TIMEOUT",
                        f"Bash 命令运行超过 {_COMMAND_TIMEOUT} 秒，已停止进程组。",
                    )
                for key, _ in selector.select(min(0.1, remaining)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    room = _MAX_OUTPUT - len(output)
                    output.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated = True
                        self._stop(process)
                        selector.unregister(key.fileobj)
                        break
                if process.poll() is not None and not selector.get_map():
                    break
            process.wait(timeout=1)
        except ToolTimeout:
            self._stop(process)
            raise
        except subprocess.TimeoutExpired as error:
            self._stop(process)
            raise ToolTimeout("TOOL_TIMEOUT", "Bash 命令未能按时退出，已停止进程组。") from error
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                self._stop(process)

        text = output.decode("utf-8", errors="replace").strip()
        if truncated:
            raise ToolFailure("BASH_OUTPUT_LIMIT", text + "\n[输出超过 16 KiB，进程组已停止。]")
        if process.returncode:
            details = f"Bash 退出码：{process.returncode}"
            if text:
                details += f"\n{text}"
            raise ToolFailure(f"BASH_EXIT_{process.returncode}", details)
        return text or "命令执行成功，没有输出。"

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
