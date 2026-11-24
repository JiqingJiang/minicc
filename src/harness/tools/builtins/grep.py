"""Search bounded UTF-8 workspace files with a regular expression."""

from collections.abc import Callable
import fnmatch
import regex

from src.harness.permission.workspace import Workspace, WorkspaceError
from src.harness.tools.base import BaseTool, ToolFailure


class GrepTool(BaseTool):
    name = "grep"
    description = "Search files by regex."
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
            "path": {"type": "string", "maxLength": 4096, "default": "."},
            "glob": {"type": "string", "maxLength": 4096},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        raw_pattern = arguments["pattern"]
        try:
            pattern = regex.compile(raw_pattern)
        except regex.error as error:
            raise ToolFailure("INVALID_PATTERN", f"正则表达式无效：{error.msg}") from error
        base = arguments.get("path", ".")
        glob = arguments.get("glob")
        if glob and (glob.startswith("/") or ".." in glob.replace("\\", "/").split("/")):
            raise ToolFailure("INVALID_PATTERN", "glob 只能匹配工作区内的相对路径。")
        try:
            if base != "." and self.workspace.is_file(base):
                files = [self.workspace.parts(base)]
                candidates = [base]
            else:
                files = [self.workspace.parts(item) for item in self.workspace.files(base, checkpoint)]
                candidates = [self.workspace.display(parts) for parts in files]
            matches: list[str] = []
            scanned_bytes = 0
            for _parts, path in zip(files, candidates):
                checkpoint()
                if glob and not _matches(path, base, glob):
                    continue
                try:
                    text = self.workspace.snapshot(path).text
                except WorkspaceError as error:
                    if error.code in {"NOT_TEXT", "TOO_LARGE"}:
                        continue
                    raise
                scanned_bytes += len(text.encode("utf-8"))
                if scanned_bytes > 8 * 1024 * 1024:
                    if matches:
                        return "\n".join(matches) + "\n[扫描达到 8 MiB 上限。]"
                    return "扫描达到 8 MiB 上限，未找到匹配文本。"
                for line_no, line in enumerate(text.splitlines(), 1):
                    checkpoint()
                    try:
                        matched = pattern.search(line, timeout=0.05)
                    except TimeoutError as error:
                        raise ToolFailure(
                            "REGEX_TIMEOUT", "正则表达式匹配超时，请简化表达式或缩小搜索范围。",
                        ) from error
                    if matched:
                        match = f"{path}:{line_no}: {line[:1_000]}"
                        if sum(len(item) for item in matches) + len(match) > 7_500:
                            return "\n".join(matches) + "\n[结果达到 7,500 字符上限。]"
                        matches.append(match)
                        if len(matches) >= 100:
                            return "\n".join(matches) + "\n[结果已限制为前 100 条。]"
            return "\n".join(matches) if matches else "没有找到匹配文本。"
        except WorkspaceError as error:
            if error.code in {"NOT_TEXT", "TOO_LARGE"}:
                return "没有找到匹配文本。"
            raise ToolFailure(error.code, str(error)) from error


def _matches(path: str, base: str, pattern: str) -> bool:
    relative = path.removeprefix(base.rstrip("./") + "/") if base != "." else path
    patterns = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
    return any(fnmatch.fnmatchcase(candidate, item) for candidate in (path, relative) for item in patterns)
