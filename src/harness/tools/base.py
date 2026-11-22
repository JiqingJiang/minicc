"""Shared tool contracts and data passed through the agent loop."""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class ToolFailure(Exception):
    """Expected, safe-to-return failure from a tool operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolTimeout(Exception):
    pass


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    error_code: str | None = None

    def model_content(self) -> str:
        return f"[{self.error_code}] {self.content}" if self.error_code else self.content


class ApprovalAction(Protocol):
    @property
    def preview(self) -> str: ...

    def execute(self, checkpoint: Callable[[], None]) -> str: ...


class BaseTool(ABC):
    name: str
    description: str
    parameters: dict
    requires_approval = False

    @abstractmethod
    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        """Run validated arguments and check long operations with checkpoint."""

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction | None:
        return None


def tool_definitions(tools: tuple[BaseTool, ...]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def schema_size(tools: tuple[BaseTool, ...]) -> int:
    return len(json.dumps(tool_definitions(tools), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
