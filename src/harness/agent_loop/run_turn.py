"""Core ReAct agent loop with generator-based HITL pauses, smart deduplication, and turn budgets."""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal, TypedDict
from uuid import uuid4

from src.harness.tools.base import BaseTool, ToolCall, ToolResult, ToolTimeout
from src.harness.tools.executor import (
    PendingApproval,
    PendingClarification,
    ToolExecutor,
    fingerprint_arguments,
)
from src.harness.tools.registry import ToolRegistry


class _MessageContent(TypedDict, total=False):
    tool_calls: tuple[ToolCall, ...]
    call_id: str
    _source_turn_id: str


class Message(_MessageContent):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None


@dataclass(frozen=True)
class RequestUsage:
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    tool_schema_bytes: int


@dataclass(frozen=True)
class ModelReply:
    content: str | None
    calls: tuple[ToolCall, ...] = ()
    usage: RequestUsage | None = None


class ModelRequestError(RuntimeError):
    pass


Complete = Callable[[list[Message], tuple[BaseTool, ...], float], ModelReply]


@dataclass(frozen=True)
class ApprovalRequest:
    call: ToolCall
    preview: str
    kind: str = "tool"
    elevated: bool = False


@dataclass(frozen=True)
class ClarificationRequest:
    call: ToolCall
    question: str
    options: tuple[str, ...] = ()
    allow_custom: bool = True


@dataclass(frozen=True)
class RetryRequest:
    message: str


TurnPause = ApprovalRequest | ClarificationRequest | RetryRequest
TurnDriver = Generator[TurnPause, Any, str]

_TOOL_APPROVAL_POLICY = (
    "启用工作区工具时，MiniCC 提供 read、write、edit、tool_search、bash、glob、grep；"
    "还可通过 update_plan 管理任务计划、ask_user 向用户提问、delegate_subagent 委派子 Agent，"
    "以及通过 search_skills、install_skill 和 load_skill 管理本机 Skill。"
    "创建或覆盖文件调用 write，精确修改现有文件调用 edit；命令操作调用 bash。"
    "Default 权限模式下，write、edit、install_skill 和每一条 bash 命令会触发宿主审批；用户可显式切换本会话 Auto 模式，"
    "让宿主自动批准沙箱内操作，但 allow_unsandboxed=true 的提权请求始终需要用户明确审批。"
    "不要只用文字声称正在等待批准；只有宿主实际暂停并返回审批请求后，才可以说操作正在等待用户决定。"
    "Default 模式下获批前不得声称副作用已执行，也不得绕过宿主权限策略。"
    "仅当用户明确提出查找或安装 Skill 时，才调用 search_skills；只有用户明确要求安装时才调用 install_skill，安装后再用 load_skill 读取。"
    "不要自动扫描其他 Agent 的本地目录，也不要自动执行已安装 Skill 中的脚本。"
    "memory_remember 按工具说明自动记录有长期价值的信息；memory_search 用于自动召回之外的定向查询。"
    "普通闲聊不要写入记忆。"
)


@dataclass(frozen=True)
class TurnLimits:
    requests: int = 40
    tool_attempts: int = 80
    seconds: float = 900


@dataclass
class TurnRecord:
    messages: list[Message]
    turn_id: str = field(default_factory=lambda: uuid4().hex)
    usage: list[RequestUsage] = field(default_factory=list)
    model_requests: int = 0
    context_traces: list[dict] = field(default_factory=list)


def _fingerprint(call: ToolCall) -> tuple[str, str]:
    return call.name, fingerprint_arguments(call.arguments)


def run_turn(
    history: list[Message],
    turn: TurnRecord,
    complete: Complete,
    registry: ToolRegistry | None,
    limits: TurnLimits,
    on_tool_start: Callable[[ToolCall, dict], None] | None = None,
    on_tool_result: Callable[[ToolCall, ToolResult], None] | None = None,
    assemble_context: Callable[[list[Message], tuple[BaseTool, ...]], tuple[list[Message], dict]] | None = None,
    approval_policy: Callable[[], str] | None = None,
    mode: str = "normal",
) -> TurnDriver:
    deadline = monotonic() + limits.seconds

    def checkpoint() -> None:
        if monotonic() >= deadline:
            raise ToolTimeout("TURN_TIMEOUT", "本轮已达到执行时间上限。")

    def pause(event: TurnPause) -> Generator[TurnPause, Any, Any]:
        nonlocal deadline
        started = monotonic()
        decision = yield event
        deadline += monotonic() - started
        return decision

    executor = ToolExecutor(on_tool_start)
    available = executor.offered_tools(registry.all(mode=mode)) if registry is not None else ()
    seen_ids: set[str] = {
        call.call_id
        for message in turn.messages
        for call in message.get("tool_calls", ())
    }
    denied: set[tuple[str, str]] = set()
    # Track active approved file mutations; running bash or modifying another file clears stale lockouts
    # so "run test -> edit code -> run same test again" works naturally.
    active_approved_writes: set[tuple[str, str]] = set()
    attempts = sum(1 for message in turn.messages if message["role"] == "tool")
    last_call = None
    last_result = None
    repeats = 0

    while turn.model_requests < limits.requests:
        final_only = attempts >= limits.tool_attempts or turn.model_requests == limits.requests - 1
        offered = () if final_only else available
        messages = [
            *([{"role": "system", "content": _TOOL_APPROVAL_POLICY}] if registry is not None else []),
            *history,
            *turn.messages,
        ]
        if final_only and registry is not None:
            messages.append({"role": "user", "content": "本轮工具阶段已结束。请根据已有结果报告进展和未完成事项。"})
        trace = {}
        if assemble_context is not None:
            messages, trace = assemble_context(messages, offered)
            turn.context_traces.append(trace)
        try:
            checkpoint()
            turn.model_requests += 1
            reply = complete(messages, offered, min(60, max(0.001, deadline - monotonic())))
            if reply.usage is not None:
                turn.usage.append(reply.usage)
                if trace:
                    trace["model"] = reply.usage.model
                    trace["actual_input_tokens"] = reply.usage.input_tokens
                    trace["actual_output_tokens"] = reply.usage.output_tokens
                    trace["cached_input_tokens"] = reply.usage.cached_input_tokens
                    trace["tool_schema_bytes"] = reply.usage.tool_schema_bytes
            if trace and callable(trace.get("_record")):
                trace.pop("_record")(trace)
            checkpoint()
        except ToolTimeout:
            if trace and callable(trace.get("_record")):
                trace.pop("_record")(trace)
            return "本轮已达到执行时间上限。已完成的工具结果已保留。"
        except ModelRequestError as error:
            if trace and callable(trace.get("_record")):
                trace.pop("_record")(trace)
            if final_only:
                return f"本轮停止。{error}已完成的工具结果已保留。"
            yield from pause(RetryRequest(str(error)))
            continue

        if not reply.calls:
            if not isinstance(reply.content, str) or not reply.content.strip():
                raise ValueError("模型没有返回可显示的回复。")
            return reply.content
        if final_only or registry is None:
            return "本轮已达到工具预算上限。已完成的工具结果已保留。"

        batch_ids = {call.call_id for call in reply.calls}
        if "" in batch_ids or len(batch_ids) != len(reply.calls) or seen_ids & batch_ids:
            raise ValueError("模型返回了无效或重复的工具调用 ID，本轮已停止。")
        seen_ids.update(batch_ids)
        turn.messages.append({"role": "assistant", "content": reply.content, "tool_calls": reply.calls})
        timed_out = False
        for call in reply.calls:
            key = _fingerprint(call)
            same_call = key == last_call
            if monotonic() >= deadline:
                outcome = ToolResult(call.call_id, "本轮已达到执行时间上限。", "TURN_TIMEOUT")
            elif attempts >= limits.tool_attempts:
                outcome = ToolResult(call.call_id, "本轮剩余工具预算不足。", "TOOL_BUDGET")
            else:
                attempts += 1
                if key in denied:
                    outcome = ToolResult(call.call_id, "用户已拒绝这个操作。请换一种做法。", "APPROVAL_DENIED")
                elif key in active_approved_writes and same_call:
                    outcome = ToolResult(call.call_id, "本轮已执行过这项获批操作。请检查已有结果。", "DUPLICATE_APPROVED_CALL")
                elif same_call and repeats >= 3:
                    outcome = ToolResult(call.call_id, "相同调用多次没有新结果。请换一种做法。", "REPEATED_CALL")
                else:
                    step = executor.execute(call, offered, checkpoint)
                    if isinstance(step, PendingApproval):
                        policy = approval_policy() if approval_policy is not None else "ask"
                        # Elevated requests (e.g. allow_unsandboxed=True or plan confirmation) always require explicit ask in interactive mode
                        if step.elevated or step.kind == "plan":
                            if policy == "deny":
                                approved = False
                            else:
                                decision = yield from pause(
                                    ApprovalRequest(step.call, step.preview, kind=step.kind, elevated=step.elevated)
                                )
                                approved = decision is True
                        elif policy == "approve":
                            approved = True
                        elif policy == "deny":
                            approved = False
                        else:
                            decision = yield from pause(
                                ApprovalRequest(step.call, step.preview, kind=step.kind, elevated=step.elevated)
                            )
                            approved = decision is True
                        outcome = executor.resume(step, approved, checkpoint)
                    elif isinstance(step, PendingClarification):
                        policy = approval_policy() if approval_policy is not None else "ask"
                        if policy == "deny":
                            outcome = ToolResult(
                                call.call_id,
                                "非交互模式下无法向用户提问，请根据现有上下文作出最稳妥的选择或报告不确定项。",
                                "CLARIFICATION_UNAVAILABLE",
                            )
                        else:
                            answer = yield from pause(
                                ClarificationRequest(
                                    step.call,
                                    step.question,
                                    step.options,
                                    step.allow_custom,
                                )
                            )
                            outcome = executor.resume_clarification(step, str(answer or ""))
                    else:
                        outcome = step

            if outcome.error_code == "APPROVAL_DENIED":
                denied.add(key)
            elif outcome.error_code == "TURN_TIMEOUT":
                timed_out = True
            elif outcome.error_code is None:
                if call.name in {"write", "edit"}:
                    active_approved_writes.add(key)
                elif call.name == "bash":
                    # Running a command may inspect or alter state; allow subsequent re-runs
                    active_approved_writes.clear()

            result = outcome.model_content()
            if not (same_call and repeats >= 3):
                repeats = repeats + 1 if same_call and result == last_result else 1
                last_call, last_result = key, result
            if repeats == 3 and result == last_result and not result.startswith("[REPEATED_CALL]"):
                result += "\n该调用已多次得到相同结果，请换一种做法。"
            if on_tool_result is not None:
                try:
                    on_tool_result(call, outcome)
                except Exception:
                    pass
            turn.messages.append({"role": "tool", "content": result, "call_id": outcome.call_id})

        if timed_out:
            return "本轮已达到执行时间上限。已完成的工具结果已保留。"

    return "本轮已达到模型请求上限。"
