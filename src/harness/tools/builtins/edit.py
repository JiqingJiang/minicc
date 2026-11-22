"""Replace one exact, unique text span after explicit approval."""

import difflib
from collections.abc import Callable
from dataclasses import dataclass

from src.harness.permission.workspace import FileSnapshot, Workspace, WorkspaceError
from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure


class EditTool(BaseTool):
    name = "edit"
    description = "Replace one exact and unique text span in a workspace file after approval."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "old_text": {"type": "string", "minLength": 1, "maxLength": 200_000},
            "new_text": {"type": "string", "maxLength": 200_000},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }
    requires_approval = True

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        del arguments, checkpoint
        raise ToolFailure("APPROVAL_REQUIRED", "edit 需要先经用户批准。")

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction:
        path, old, new = arguments["path"], arguments["old_text"], arguments["new_text"]
        if not old.strip() or old == new:
            raise ToolFailure("INVALID_ARGUMENTS", "old_text 不能为空，且新旧文本必须不同。")
        try:
            checkpoint()
            baseline = self.workspace.snapshot(path)
        except WorkspaceError as error:
            raise ToolFailure(error.code, str(error)) from error
        first = baseline.text.find(old)
        if first < 0 or baseline.text.find(old, first + 1) >= 0:
            raise ToolFailure("EDIT_NOT_UNIQUE", "old_text 必须在文件中恰好出现一次。")
        updated = baseline.text[:first] + new + baseline.text[first + len(old):]
        preview = "".join(difflib.unified_diff(
            baseline.text.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ))
        if len(preview.encode("utf-8")) > 8_000:
            raise ToolFailure("PREVIEW_TOO_LARGE", "编辑预览太长，请缩小修改范围。")
        return EditAction(self.workspace, path, updated, baseline, preview)


@dataclass(frozen=True)
class EditAction:
    workspace: Workspace
    path: str
    content: str
    baseline: FileSnapshot
    preview: str

    def execute(self, checkpoint: Callable[[], None]) -> str:
        try:
            self.workspace.replace(
                self.path,
                self.content,
                self.baseline.identity,
                self.baseline.digest,
                checkpoint,
            )
        except WorkspaceError as error:
            raise ToolFailure(error.code, str(error)) from error
        return f"Updated {self.path}."
