"""Persisted, user-maintained task state kept separate from chat history."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class TaskState:
    goal: str = ""
    requirements: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    updated_at: str = ""


class TaskStateStore:
    _FIELDS = {"requirement": "requirements", "decision": "decisions", "progress": "progress", "open": "open_questions"}

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> TaskState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TaskState()
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"无法读取任务状态 {self.path}: {error}") from error
        try:
            return TaskState(**data)
        except (TypeError, KeyError) as error:
            raise RuntimeError(f"任务状态格式无效：{self.path}") from error

    def set_goal(self, goal: str) -> TaskState:
        state = self.load()
        state.goal = goal.strip()
        return self._save(state)

    def add(self, kind: str, text: str) -> TaskState:
        field_name = self._FIELDS.get(kind)
        if field_name is None or not text.strip():
            raise ValueError("请提供 requirement、decision、progress、open 类型及内容。")
        state = self.load()
        values = getattr(state, field_name)
        values.append(text.strip())
        return self._save(state)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def _save(self, state: TaskState) -> TaskState:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        return state
