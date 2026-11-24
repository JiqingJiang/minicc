"""Tool registration, mode-aware lookup, and persistent enable/disable controls."""

from __future__ import annotations

from collections.abc import Iterable
import json
from pathlib import Path

from src.adapters.storage.json_file import atomic_write_json, file_lock
from src.harness.tools.base import BaseTool

_PLAN_ALLOWED_NAMES = {"update_plan", "ask_user", "delegate_subagent"}


class ToolStateStore:
    """Persist disabled tool names in ~/.minicc/tools.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def disabled(self) -> set[str]:
        if self.path is None:
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return set()
        if not isinstance(raw, dict):
            return set()
        values = raw.get("disabled", [])
        return {item for item in values if isinstance(item, str)} if isinstance(values, list) else set()

    def set_disabled(self, disabled_names: set[str]) -> None:
        if self.path is None:
            return
        with file_lock(self.path):
            atomic_write_json(self.path, {"disabled": sorted(disabled_names)})


class ToolRegistry:
    """Keep tools in stable registration order, manage enabled/disabled state, and look them up by name."""

    def __init__(self, tools: Iterable[BaseTool] = (), *, state_path: Path | None = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._state = ToolStateStore(state_path)
        self._disabled: set[str] = self._state.disabled()
        for tool in tools:
            self.register(tool)

    def bind_state_path(self, state_path: Path) -> None:
        self._state = ToolStateStore(state_path)
        self._disabled = self._state.disabled()

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str, *, include_disabled: bool = False) -> BaseTool | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        if not include_disabled and name in self._disabled:
            return None
        return tool

    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self._tools:
            return False
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        self._state.set_disabled(self._disabled)
        return True

    def toggle(self, name: str) -> bool | None:
        if name not in self._tools:
            return None
        new_state = not self.is_enabled(name)
        self.set_enabled(name, new_state)
        return new_state

    def all_registered(self) -> tuple[tuple[BaseTool, bool], ...]:
        """Return all registered tools paired with their enabled status."""
        return tuple((tool, tool.name not in self._disabled) for tool in self._tools.values())

    def all(self, *, mode: str = "normal") -> tuple[BaseTool, ...]:
        """Return enabled tools in their stable registration order, filtered by runtime mode."""
        tools = tuple(tool for tool in self._tools.values() if tool.name not in self._disabled)
        if mode == "plan":
            return tuple(
                tool
                for tool in tools
                if getattr(tool, "read_only", False) or tool.name in _PLAN_ALLOWED_NAMES
            )
        return tools
