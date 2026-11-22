from dataclasses import dataclass
from collections.abc import Callable
from threading import Lock

from src.harness.agent_loop.run_turn import (
    ApprovalRequest,
    Complete,
    Message,
    RetryRequest,
    TurnDriver,
    TurnLimits,
    TurnPause,
    TurnRecord,
    run_turn,
)
from src.harness.tools.registry import ToolRegistry
from src.harness.tools.base import ToolCall, ToolResult


@dataclass
class _ActiveTurn:
    record: TurnRecord
    driver: TurnDriver
    waiting: TurnPause | None = None


class ChatSession:
    def __init__(
        self,
        complete: Complete,
        registry: ToolRegistry | None = None,
        limits: TurnLimits = TurnLimits(),
        on_tool_start: Callable[[ToolCall, dict], None] | None = None,
        on_tool_result: Callable[[ToolCall, ToolResult], None] | None = None,
    ) -> None:
        self._complete = complete
        self._registry = registry
        self._limits = limits
        self._on_tool_start = on_tool_start
        self._on_tool_result = on_tool_result
        self._history: list[TurnRecord] = []
        self._active: _ActiveTurn | None = None
        self._turn_lock = Lock()

    def set_model(self, complete: Complete) -> None:
        with self._turn_lock:
            if self._active is not None:
                raise RuntimeError("请先完成当前轮次，或用 /cancel 结束本轮。")
            self._complete = complete

    def set_tool_observer(self, observer: Callable[[ToolCall, dict], None] | None) -> None:
        with self._turn_lock:
            self._on_tool_start = observer

    def set_tool_result_observer(
        self, observer: Callable[[ToolCall, ToolResult], None] | None,
    ) -> None:
        with self._turn_lock:
            self._on_tool_result = observer

    def send(self, content: str) -> str | TurnPause:
        if not content.strip():
            raise ValueError("请输入消息。")
        if not self._turn_lock.acquire(blocking=False):
            raise RuntimeError("请等当前回复结束。")

        try:
            if self._active is not None:
                raise RuntimeError("本轮尚未完成，请继续处理或输入 /cancel。")
            history = [
                message
                for record in self._history
                for message in (record.messages[0], record.messages[-1])
            ]
            record = TurnRecord(messages=[{"role": "user", "content": content}])
            driver = run_turn(
                history,
                record,
                self._complete,
                self._registry,
                self._limits,
                self._on_tool_start,
                self._on_tool_result,
            )
            self._active = _ActiveTurn(record, driver)
            return self._advance(self._active)
        finally:
            self._turn_lock.release()

    def answer_approval(self, approved: bool) -> str | TurnPause:
        with self._turn_lock:
            turn = self._active
            if turn is None or not isinstance(turn.waiting, ApprovalRequest):
                raise RuntimeError("当前没有等待审批的操作。")
            return self._advance(turn, approved)

    def retry(self) -> str | TurnPause:
        with self._turn_lock:
            turn = self._active
            if turn is None or not isinstance(turn.waiting, RetryRequest):
                raise RuntimeError("当前没有可以重试的模型请求。")
            return self._advance(turn)

    def cancel(self) -> bool:
        with self._turn_lock:
            if self._active is None:
                return False
            self._active.driver.close()
            self._settle_partial(self._active)
            return True

    def _advance(self, turn: _ActiveTurn, decision: bool | None = None) -> str | TurnPause:
        try:
            turn.waiting = turn.driver.send(decision)
            return turn.waiting
        except StopIteration as done:
            reply = done.value
            turn.record.messages.append({"role": "assistant", "content": reply})
            self._history.append(turn.record)
            self._active = None
            return reply
        except (Exception, KeyboardInterrupt):
            self._settle_partial(turn)
            raise

    def _settle_partial(self, turn: _ActiveTurn) -> None:
        messages = turn.record.messages
        results = {message["call_id"] for message in messages if message["role"] == "tool"}
        for call in [call for message in messages for call in message.get("tool_calls", ())]:
            if call.call_id not in results:
                skipped: Message = {
                    "role": "tool",
                    "content": "[CANCELLED] 本轮已中止，未记录该调用的最终结果。",
                    "call_id": call.call_id,
                }
                messages.append(skipped)
        if len(messages) > 1:
            messages.append({"role": "assistant", "content": "本轮已中止，工具记录已保留。"})
            self._history.append(turn.record)
        self._active = None
