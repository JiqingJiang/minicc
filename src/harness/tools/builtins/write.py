"""Create or replace a workspace text file after explicit approval."""

import difflib
from collections.abc import Callable
from dataclasses import dataclass

from src.harness.permission.workspace import FileSnapshot, Workspace, WorkspaceError
from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure


class WriteTool(BaseTool):
    name = "write"
    description = "Create or replace a UTF-8 text file in the workspace after approval."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string", "maxLength": 1_048_576},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    requires_approval = True

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        del arguments, checkpoint
        raise ToolFailure("APPROVAL_REQUIRED", "write 需要先经用户批准。")

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction:
        path, content = arguments["path"], arguments["content"]
        checkpoint()
        try:
            current = self.workspace.snapshot(path)
        except WorkspaceError as error:
            if error.code != "NOT_FOUND":
                raise ToolFailure(error.code, str(error)) from error
            return WriteAction(self.workspace, path, content, None, _diff("", content, path))
        if current.text == content:
            raise ToolFailure("UNCHANGED", "文件内容没有变化。")
        return WriteAction(self.workspace, path, content, current, _diff(current.text, content, path))


@dataclass(frozen=True)
class WriteAction:
    workspace: Workspace
    path: str
    content: str
    baseline: FileSnapshot | None
    preview: str

    def execute(self, checkpoint: Callable[[], None]) -> str:
        try:
            if self.baseline is None:
                self.workspace.create(self.path, self.content, checkpoint)
                return f"Created {self.path}."
            self.workspace.replace(
                self.path,
                self.content,
                self.baseline.identity,
                self.baseline.digest,
                checkpoint,
            )
            return f"Updated {self.path}."
        except WorkspaceError as error:
            raise ToolFailure(error.code, str(error)) from error


def _diff(before: str, after: str, path: str) -> str:
    preview = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))
    if len(preview.encode("utf-8")) > 8_000:
        raise ToolFailure("PREVIEW_TOO_LARGE", "写入预览太长，请缩小修改范围。")
    return preview or f"将创建空文件：{path}"
