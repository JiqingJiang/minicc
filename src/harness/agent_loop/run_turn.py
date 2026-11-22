from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal, TypedDict

from src.harness.tools.executor import PendingApproval, ToolExecutor, fingerprint_arguments
from src.harness.tools.registry import ToolRegistry
from src.harness.tools.base import BaseTool, ToolCall, ToolResult, ToolTimeout


class _MessageContent(TypedDict, total=False):
    tool_calls: tuple[ToolCall, ...]
    call_id: str


class Message(_MessageContent):
    role: Literal["user", "assistant", "tool"]
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


@dataclass(frozen=True)
class RetryRequest:
    message: str


TurnPause = ApprovalRequest | RetryRequest
TurnDriver = Generator[TurnPause, bool | None, str]

_TOOL_APPROVAL_POLICY = (
    "MiniCC 当前默认提供 read、write、edit、tool_search、bash、glob、grep 七种工具。"
    "创建或覆盖文件调用 write，精确修改现有文件调用 edit；命令操作调用 bash。"
    "write、edit 和每一条 bash 命令都会触发宿主真实审批。不要只用文字声称正在等待批准；"
    "只有宿主返回实际审批请求后，才可以说操作正在等待用户决定。宿主会展示预览或完整命令并暂停。"
    "在获批前不得声称副作用已执行，也不得绕过审批。"
)


@dataclass(frozen=True)
class TurnLimits:
    requests: int = 12
    tool_attempts: int = 24
    seconds: float = 180


@dataclass
class TurnRecord:
    messages: list[Message]
    usage: list[RequestUsage] = field(default_factory=list)
    model_requests: int = 0


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
) -> TurnDriver:
    deadline = monotonic() + limits.seconds

    def checkpoint() -> None:
        if monotonic() >= deadline:
            raise ToolTimeout

    def pause(event: TurnPause) -> Generator[TurnPause, bool | None, bool]:
        nonlocal deadline
        started = monotonic()
        decision = yield event
        deadline += monotonic() - started
        return decision is True

    executor = ToolExecutor(on_tool_start)
    available = executor.offered_tools(registry.all()) if registry is not None else ()
    seen_ids: set[str] = set()
    approved_calls: set[tuple[str, str]] = set()
    denied: set[tuple[str, str]] = set()
    attempts = 0
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
        try:
            checkpoint()
            turn.model_requests += 1
            reply = complete(messages, offered, min(60, max(0.001, deadline - monotonic())))
            if reply.usage is not None:
                turn.usage.append(reply.usage)
            checkpoint()
        except ToolTimeout:
            return "本轮已达到执行时间上限。已完成的工具结果已保留。"
        except ModelRequestError as error:
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
        approval_names = {tool.name for tool in offered if tool.requires_approval}
        for call in reply.calls:
            key = _fingerprint(call)
            same_call = key == last_call
            if monotonic() >= deadline:
                outcome = ToolResult(call.call_id, "本轮执行时间已用完。", "TOOL_TIMEOUT")
            elif attempts >= limits.tool_attempts:
                outcome = ToolResult(call.call_id, "本轮剩余工具预算不足。", "TOOL_BUDGET")
            else:
                attempts += 1
                if key in denied:
                    outcome = ToolResult(call.call_id, "用户已拒绝这个操作。请换一种做法。", "APPROVAL_DENIED")
                elif key in approved_calls:
                    outcome = ToolResult(call.call_id, "本轮已执行过这项获批操作。请检查已有结果。", "DUPLICATE_APPROVED_CALL")
                elif same_call and repeats >= 3:
                    outcome = ToolResult(call.call_id, "相同调用多次没有新结果。请换一种做法。", "REPEATED_CALL")
                else:
                    outcome = executor.execute(call, offered, checkpoint)
                    if isinstance(outcome, PendingApproval):
                        approved = yield from pause(ApprovalRequest(outcome.call, outcome.preview))
                        outcome = executor.resume(outcome, approved, checkpoint)

            if outcome.error_code == "APPROVAL_DENIED":
                denied.add(key)
            elif outcome.error_code == "TOOL_TIMEOUT":
                timed_out = True
            elif outcome.error_code is None and call.name in approval_names:
                approved_calls.add(key)
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
                    # Terminal output is observational and must not affect the turn.
                    pass
            turn.messages.append({"role": "tool", "content": result, "call_id": outcome.call_id})

        if timed_out:
            return "本轮已达到执行时间上限。已完成的工具结果已保留。"

    return "本轮已达到模型请求上限。"
