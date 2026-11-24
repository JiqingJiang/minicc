"""Isolated-context SubAgent delegation (explorer, planner, coder, verifier)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.harness.agent_loop.run_turn import (
    ApprovalRequest,
    ClarificationRequest,
    Complete,
    RetryRequest,
    TurnLimits,
    TurnRecord,
    run_turn,
)
from src.harness.tools.base import BaseTool, ToolFailure
from src.harness.tools.registry import ToolRegistry

SUBAGENT_ROLES = {"explorer", "planner", "coder", "verifier"}
_READ_ONLY_ROLES = {"explorer", "planner"}

_ROLE_PROMPTS = {
    "explorer": (
        "你是 MiniCC 的 Explorer 子 Agent。你的职责是使用只读工具（read、glob、grep、web_search、web_fetch、memory_search、load_skill）"
        "快速定位代码实现、查找引用或检索网页资料，并向父 Agent 返回简洁、带文件路径/行号或 URL 证据的调查结论。不要猜测未读取的内容。"
    ),
    "planner": (
        "你是 MiniCC 的 Planner 子 Agent。你的职责是使用只读工具分析现有代码结构与约束，"
        "向父 Agent 提供清晰的实施步骤、边界条件与验证建议。"
    ),
    "coder": (
        "你是 MiniCC 的 Coder 子 Agent。你的职责是在分配的局部范围内完成代码创建或修改，并向父 Agent 报告实际改动的文件与结果。"
    ),
    "verifier": (
        "你是 MiniCC 的 Verifier 子 Agent。你的职责是运行指定的测试或核查文件状态，向父 Agent 如实报告通过/失败证据。"
    ),
}


@dataclass(frozen=True)
class SubAgentResult:
    role: str
    summary: str
    model_requests: int
    tool_calls: int
    input_tokens: int
    output_tokens: int


class SubAgentRunner:
    """Execute a short-lived child agent turn with an isolated context window."""

    def __init__(
        self,
        complete_getter: Callable[[], Complete],
        registry: ToolRegistry,
        *,
        approval_policy_getter: Callable[[], Callable[[], str] | None] | None = None,
        mode_getter: Callable[[], str] | None = None,
        limits: TurnLimits = TurnLimits(requests=12, tool_attempts=24, seconds=180),
    ) -> None:
        self._complete_getter = complete_getter
        self._registry = registry
        self._approval_policy_getter = approval_policy_getter
        self._mode_getter = mode_getter
        self._limits = limits

    def run(self, role: str, task: str, context: str = "") -> SubAgentResult:
        if role not in SUBAGENT_ROLES:
            raise ValueError(f"不支持的子 Agent 角色：{role}")
        parent_mode = self._mode_getter() if self._mode_getter is not None else "normal"
        if parent_mode == "plan" and role not in _READ_ONLY_ROLES:
            raise ToolFailure("SUBAGENT_ROLE_DENIED", "Plan 模式下仅允许调用只读子 Agent（explorer 或 planner）。")

        child_registry = self._build_child_registry(role)
        prompt_parts = [f"【子任务（角色：{role}）】\n{task.strip()}"]
        if context.strip():
            prompt_parts.append(f"【父 Agent 提供的背景与上下文】\n{context.strip()}")
        record = TurnRecord(messages=[{"role": "user", "content": "\n\n".join(prompt_parts)}])
        system_history = [{"role": "system", "content": _ROLE_PROMPTS[role]}]
        policy = self._approval_policy_getter() if self._approval_policy_getter is not None else None

        driver = run_turn(
            system_history,
            record,
            self._complete_getter(),
            child_registry,
            self._limits,
            approval_policy=policy,
            mode="plan" if role in _READ_ONLY_ROLES else "normal",
        )
        decision = None
        while True:
            try:
                pause = driver.send(decision)
            except StopIteration as done:
                reply = done.value
                break
            if isinstance(pause, ApprovalRequest):
                current_policy = policy() if policy is not None else "ask"
                decision = current_policy == "approve" and not pause.elevated
            elif isinstance(pause, ClarificationRequest):
                decision = "请基于现有代码证据自行判断并在报告中注明不确定项。"
            elif isinstance(pause, RetryRequest):
                driver.close()
                raise ToolFailure("SUBAGENT_MODEL_ERROR", f"子 Agent 模型请求失败：{pause.message}")

        tool_count = sum(1 for msg in record.messages if msg["role"] == "tool")
        in_tokens = sum(u.input_tokens or 0 for u in record.usage)
        out_tokens = sum(u.output_tokens or 0 for u in record.usage)
        return SubAgentResult(
            role=role,
            summary=reply,
            model_requests=record.model_requests,
            tool_calls=tool_count,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

    def _build_child_registry(self, role: str) -> ToolRegistry:
        child = ToolRegistry()
        for tool in self._registry.all():
            # Prevent recursion and interactive user blocking inside subagents
            if tool.name in {"delegate_subagent", "ask_user", "update_plan", "install_skill"}:
                continue
            if role in _READ_ONLY_ROLES and not getattr(tool, "read_only", False):
                continue
            if role == "verifier" and not (getattr(tool, "read_only", False) or tool.name == "bash"):
                continue
            child.register(tool)
        return child


class DelegateSubAgentTool(BaseTool):
    name = "delegate_subagent"
    description = (
        "Delegate a focused subtask to an isolated SubAgent (explorer, planner, coder, or verifier) "
        "so its intermediate file reads and searches do not bloat the main conversation context."
    )
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": sorted(SUBAGENT_ROLES)},
            "task": {"type": "string", "minLength": 4, "maxLength": 4000},
            "context": {"type": "string", "maxLength": 4000},
        },
        "required": ["role", "task"],
        "additionalProperties": False,
    }

    def __init__(self, runner: SubAgentRunner) -> None:
        self.runner = runner

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        role = arguments["role"]
        task = arguments["task"]
        context = arguments.get("context", "")
        result = self.runner.run(role, task, context)
        checkpoint()
        header = (
            f"[SubAgent {result.role} 完成 · 模型请求={result.model_requests} · "
            f"工具调用={result.tool_calls} · Tokens={result.input_tokens} in / {result.output_tokens} out]"
        )
        return f"{header}\n{result.summary}"
