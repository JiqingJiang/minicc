"""Approval-gated Bash command execution."""

import os
import selectors
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure, ToolTimeout


_MAX_COMMAND = 7_000
_MAX_OUTPUT = 16 * 1024
_COMMAND_TIMEOUT = 60


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a Bash command with user approval. It starts in the workspace but runs as the current user "
        "and can access files outside the workspace and use the network."
    )
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1, "maxLength": _MAX_COMMAND}},
        "required": ["command"],
        "additionalProperties": False,
    }
    requires_approval = True

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd).resolve(strict=True)
        if not self.cwd.is_dir():
            raise ValueError("Bash 工作目录必须是目录。")

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        del arguments, checkpoint
        raise ToolFailure("APPROVAL_REQUIRED", "bash 命令需要先经用户批准。")

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction:
        command = arguments["command"]
        shell = shutil.which("bash")
        if shell is None:
            raise ToolFailure("BASH_UNAVAILABLE", "系统中找不到 Bash。")
        checkpoint()
        preview = (
            f"工作目录：{self.cwd}\n命令：\n{command}\n\n"
            "此命令以当前用户权限运行，可访问工作区外的文件和网络；系统没有限制它只能操作工作区。"
        )
        return BashAction(self.cwd, shell, command, preview)


@dataclass(frozen=True)
class BashAction:
    cwd: Path
    shell: str
    command: str
    preview: str

    def execute(self, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            process = subprocess.Popen(
                [self.shell, "--noprofile", "--norc", "-c", self.command],
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
                    raise ToolTimeout
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
        except (ToolTimeout, subprocess.TimeoutExpired):
            self._stop(process)
            raise ToolTimeout
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
