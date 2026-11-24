"""Persisted task, goal, and plan state kept separate from chat history."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path

from src.adapters.storage.json_file import atomic_write_json, file_lock

PLAN_STEP_STATUSES = {"pending", "in_progress", "completed"}


@dataclass
class PlanStep:
    title: str
    status: str = "pending"

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("计划步骤标题不能为空。")
        if self.status not in PLAN_STEP_STATUSES:
            raise ValueError(f"计划步骤状态必须是 {', '.join(sorted(PLAN_STEP_STATUSES))}。")


@dataclass
class TaskState:
    goal: str = ""
    requirements: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    plan_steps: list[PlanStep] = field(default_factory=list)
    verification_command: str = ""
    plan_approved: bool = False
    updated_at: str = ""

    @property
    def has_incomplete_steps(self) -> bool:
        return bool(self.plan_steps) and any(step.status != "completed" for step in self.plan_steps)


class TaskStateStore:
    _FIELDS = {
        "requirement": "requirements",
        "decision": "decisions",
        "progress": "progress",
        "open": "open_questions",
    }

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> TaskState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TaskState()
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"无法读取任务状态 {self.path}: {error}") from error
        if not isinstance(data, dict):
            raise RuntimeError(f"任务状态格式无效：{self.path}")
        try:
            raw_steps = data.get("plan_steps", [])
            steps = [
                PlanStep(title=item["title"], status=item.get("status", "pending"))
                if isinstance(item, dict)
                else PlanStep(title=str(item))
                for item in (raw_steps if isinstance(raw_steps, list) else [])
            ]
            return TaskState(
                goal=str(data.get("goal", "")),
                requirements=list(data.get("requirements", [])),
                decisions=list(data.get("decisions", [])),
                progress=list(data.get("progress", [])),
                open_questions=list(data.get("open_questions", [])),
                plan_steps=steps,
                verification_command=str(data.get("verification_command", "")),
                plan_approved=bool(data.get("plan_approved", False)),
                updated_at=str(data.get("updated_at", "")),
            )
        except (TypeError, KeyError, ValueError) as error:
            raise RuntimeError(f"任务状态格式无效：{self.path}") from error

    def set_goal(self, goal: str, *, verification_command: str | None = None) -> TaskState:
        with file_lock(self.path):
            state = self.load()
            state.goal = goal.strip()
            if verification_command is not None:
                state.verification_command = verification_command.strip()
            return self._save_unlocked(state)

    def update_plan(
        self,
        *,
        goal: str | None = None,
        steps: list[dict] | None = None,
        verification_command: str | None = None,
        plan_approved: bool | None = None,
    ) -> TaskState:
        with file_lock(self.path):
            state = self.load()
            if goal is not None and goal.strip():
                state.goal = goal.strip()
            if steps is not None:
                state.plan_steps = [
                    PlanStep(title=str(item.get("title", "")), status=str(item.get("status", "pending")))
                    for item in steps
                ]
            if verification_command is not None:
                state.verification_command = verification_command.strip()
            if plan_approved is not None:
                state.plan_approved = bool(plan_approved)
            return self._save_unlocked(state)

    def add(self, kind: str, text: str) -> TaskState:
        field_name = self._FIELDS.get(kind)
        if field_name is None or not text.strip():
            raise ValueError("请提供 requirement、decision、progress、open 类型及内容。")
        with file_lock(self.path):
            state = self.load()
            values = getattr(state, field_name)
            values.append(text.strip())
            return self._save_unlocked(state)

    def clear(self) -> None:
        with file_lock(self.path):
            if self.path.exists():
                self.path.unlink()

    def _save_unlocked(self, state: TaskState) -> TaskState:
        state.updated_at = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.path, asdict(state))
        return state
