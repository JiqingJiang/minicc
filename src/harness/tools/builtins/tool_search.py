"""Find registered tools by name or description."""

from collections.abc import Callable
import re

from src.harness.tools.base import BaseTool, ToolFailure
from src.harness.tools.registry import ToolRegistry

_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
_CHINESE_ACTIONS = {
    "读取": "read", "阅读": "read", "写入": "write", "创建": "write", "保存": "write",
    "修改": "edit", "编辑": "edit", "搜索": "grep", "查找内容": "grep", "列出": "glob",
    "文件名": "glob", "命令": "bash", "运行": "bash", "执行": "bash",
    "计划": "update_plan", "提问": "ask_user", "子任务": "delegate_subagent",
    "上网": "web_search", "联网": "web_search", "网页搜索": "web_search", "查资料": "web_search",
    "抓取": "web_fetch", "网页": "web_fetch", "网址": "web_fetch", "链接": "web_fetch",
}


class ToolSearchTool(BaseTool):
    name = "tool_search"
    description = "Find a tool."
    read_only = True
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 400}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        from src.harness.tools.executor import ToolExecutor

        query = arguments["query"].casefold()
        tokens = [token.casefold() for token in _TOKEN.findall(query)]
        tokens.extend(tool for phrase, tool in _CHINESE_ACTIONS.items() if phrase in query)
        if not tokens:
            raise ToolFailure("INVALID_ARGUMENTS", "请描述要查找的工具。")
        matches: list[tuple[int, BaseTool]] = []
        available = ToolExecutor().offered_tools(self.registry.all())
        for tool in available:
            checkpoint()
            if tool is self:
                continue
            name = tool.name.casefold()
            description = tool.description.casefold()
            score = sum(4 if token in name else int(token in description) for token in tokens)
            if score:
                matches.append((score, tool))
        matches.sort(key=lambda item: item[0], reverse=True)
        if not matches:
            return "没有找到匹配的工具。"
        return "可用工具：\n" + "\n".join(
            f"- {tool.name}: {tool.description}" for _, tool in matches[:5]
        )
