"""Find workspace files by a bounded glob pattern."""

import fnmatch
from collections.abc import Callable

from src.harness.permission.workspace import Workspace, WorkspaceError
from src.harness.tools.base import BaseTool, ToolFailure


class GlobTool(BaseTool):
    name = "glob"
    description = "List workspace files matching a glob pattern, such as **/*.py."
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
        results = results[:500]
        return "\n".join(results) if results else "没有匹配的工作区文件。"


def _matches(path: str, base: tuple[str, ...], pattern: str) -> bool:
    parts = tuple(path.split("/"))
    relative = "/".join(parts[len(base):]) if parts[:len(base)] == base else path
    patterns = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
    return any(fnmatch.fnmatchcase(candidate, item) for candidate in (path, relative) for item in patterns)
