"""Find workspace files by a bounded glob pattern."""

from collections.abc import Callable
import fnmatch

from src.harness.permission.workspace import Workspace, WorkspaceError
from src.harness.tools.base import BaseTool, ToolFailure


class GlobTool(BaseTool):
    name = "glob"
    description = "Find files by glob."
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
            "path": {"type": "string", "maxLength": 4096, "default": "."},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        pattern = arguments["pattern"].replace("\\", "/")
        if pattern.startswith("/") or ".." in pattern.split("/"):
            raise ToolFailure("INVALID_PATTERN", "glob 只能匹配工作区内的相对路径。")
        base = arguments.get("path", ".")
        try:
            base_parts = self.workspace.parts(base, allow_root=True)
            results = [
                candidate
                for candidate in self.workspace.files(base, checkpoint)
                if _matches(candidate, base_parts, pattern)
            ]
        except WorkspaceError as error:
            raise ToolFailure(error.code, str(error)) from error
        truncated = len(results) > 500
        results = results[:500]
        if not results:
            return "没有匹配的工作区文件。"
        text = "\n".join(results)
        if truncated:
            text += "\n[结果已限制为前 500 条。]"
        return text


def _matches(path: str, base: tuple[str, ...], pattern: str) -> bool:
    parts = tuple(path.split("/"))
    relative = "/".join(parts[len(base) :]) if parts[: len(base)] == base else path
    patterns = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
    return any(fnmatch.fnmatchcase(candidate, item) for candidate in (path, relative) for item in patterns)
