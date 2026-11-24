"""Human-in-the-loop (HITL) tools for structured clarification (ask_user) and plan management (update_plan)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure
from src.harness.tools.executor import ClarificationPause
from src.runtime.task.state import PLAN_STEP_STATUSES, TaskStateStore


class AskUserTool(BaseTool):
    name = "ask_user"
    description = (
        "Ask the user a clarifying question with 2-4 choices when key requirements are ambiguous and "
        "guessing would risk doing the wrong work."
    )
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 2, "maxLength": 1000},
            "options": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 300},
                "maxItems": 6,
            },
            "allow_custom": {"type": "boolean", "default": True},
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        question = arguments["question"].strip()
        options = tuple(str(opt).strip() for opt in arguments.get("options", ()) if str(opt).strip())
        allow_custom = bool(arguments.get("allow_custom", True))
        raise ClarificationPause(question=question, options=options, allow_custom=allow_custom)


@dataclass(frozen=True)
class _PlanApprovalAction:
    store: TaskStateStore
    goal: str | None
    steps: list[dict]
    verification_command: str | None
    preview: str
    kind: str = "plan"
    elevated: bool = False

    def execute(self, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        state = self.store.update_plan(
            goal=self.goal,
            steps=self.steps,
            verification_command=self.verification_command,
            plan_approved=True,
        )
        return _format_plan_summary(state, approved=True)


class UpdatePlanTool(BaseTool):
    name = "update_plan"
    description = (
        "Create or update the structured task plan (goal, ordered steps with pending/in_progress/completed status, "
        "and optional verification command). Set submit_for_approval=true in Plan mode when ready for user review."
    )
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "minLength": 1, "maxLength": 1000},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 300},
                        "status": {"type": "string", "enum": sorted(PLAN_STEP_STATUSES)},
                    },
                    "required": ["title", "status"],
                    "additionalProperties": False,
                },
            },
            "verification_command": {"type": "string", "maxLength": 500},
            "submit_for_approval": {"type": "boolean", "default": False},
        },
        "required": ["steps"],
        "additionalProperties": False,
    }

    def __init__(self, store: TaskStateStore) -> None:
        self.store = store

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction | None:
        checkpoint()
        if not arguments.get("submit_for_approval", False):
            return None
        steps = arguments["steps"]
        goal = arguments.get("goal")
        verification_command = arguments.get("verification_command")
        lines = ["【实施计划确认】"]
        if goal:
            lines.append(f"目标：{goal}")
        lines.append("步骤：")
        for idx, item in enumerate(steps, start=1):
            lines.append(f"  {idx}. [{item['status']}] {item['title']}")
        if verification_command:
            lines.append(f"验证命令：{verification_command}")
        return _PlanApprovalAction(
            self.store,
            goal=goal,
            steps=steps,
            verification_command=verification_command,
            preview="\n".join(lines),
        )

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            state = self.store.update_plan(
                goal=arguments.get("goal"),
                steps=arguments["steps"],
                verification_command=arguments.get("verification_command"),
            )
        except ValueError as error:
            raise ToolFailure("INVALID_PLAN", str(error)) from error
        return _format_plan_summary(state, approved=state.plan_approved)


def _format_plan_summary(state, *, approved: bool) -> str:
    status_label = "已获批" if approved else "已更新"
    lines = [f"任务计划{status_label}（目标：{state.goal or '未指定'}）："]
    for idx, step in enumerate(state.plan_steps, start=1):
        lines.append(f"{idx}. [{step.status}] {step.title}")
    if state.verification_command:
        lines.append(f"验证命令：{state.verification_command}")
    return "\n".join(lines)
