"""Read bounded text from the workspace."""

from collections.abc import Callable

from src.harness.permission.workspace import Workspace, WorkspaceError
from src.harness.tools.base import BaseTool, ToolFailure


class ReadTool(BaseTool):
    name = "read"
    description = "Read workspace text."
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "line_count": {"type": "integer", "minimum": 1, "maximum": 400, "default": 100},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        try:
            checkpoint()
            path = arguments["path"]
            snapshot = self.workspace.snapshot(path)
        except WorkspaceError as error:
            raise ToolFailure(error.code, str(error)) from error
        start = arguments.get("start_line", 1)
        count = arguments.get("line_count", 100)
        lines = snapshot.text.splitlines()
        if start > max(len(lines), 1):
            raise ToolFailure("INVALID_ARGUMENTS", "start_line 超出文件范围。")
        selected = lines[start - 1 : start - 1 + count]
        output: list[str] = []
        used = 0
        for line_no, line in enumerate(selected, start=start):
            checkpoint()
            shown = f"{line_no:>5}│ {line[:2_000]}"
            if used + len(shown) > 7_500:
                break
            output.append(shown)
            used += len(shown) + 1
        end = start + len(selected) - 1
        header = f"{path} 是空文件。" if not lines else f"{path} 第 {start}-{end} 行"
        if len(output) < len(selected) or start - 1 + len(selected) < len(lines):
            header += f"（可从第 {start + len(output)} 行继续）"
        return header + ("\n" + "\n".join(output) if output else "")
