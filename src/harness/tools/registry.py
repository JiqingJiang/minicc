"""Tool registration and lookup.

The registry deliberately has one responsibility: keeping the set of tools
available to a runtime. Argument validation, permissions, execution and
request-size decisions belong to the executor/runtime layers.
"""

from collections.abc import Iterable

from src.harness.tools.base import BaseTool


class ToolRegistry:
    """Keep tools in stable registration order and look them up by name."""

    def __init__(self, tools: Iterable[BaseTool] = ()) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        """Register *tool*, rejecting duplicate names.

        The registry intentionally does not validate tool metadata or schemas.
        ``ToolExecutor`` is the single boundary responsible for validating
        arguments before an execution reaches a tool.
        """

        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all(self) -> tuple[BaseTool, ...]:
        """Return tools in their stable registration order."""

        return tuple(self._tools.values())
