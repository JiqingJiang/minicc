from dataclasses import dataclass
from collections.abc import Callable
import json
import logging
from pathlib import Path
from threading import Lock
from uuid import uuid4

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
from src.harness.context.assembler import ContextAssembler
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
        context: ContextAssembler | None = None,
        history_path: Path | None = None,
    ) -> None:
        self._complete = complete
        self._registry = registry
        self._limits = limits
        self._on_tool_start = on_tool_start
        self._on_tool_result = on_tool_result
        self._context = context
        self._history_path = history_path
        self._session_id, self._history = self._load_history(history_path)
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
                {**message, "_source_turn_id": record.turn_id}
                for record in self._history
                for message in record.messages
            ]
            record = TurnRecord(messages=[{"role": "user", "content": content}])
            memory_tool = self._registry.get("memory_remember") if self._registry is not None else None
            if memory_tool is not None and hasattr(memory_tool, "set_source_ref"):
                memory_tool.set_source_ref(self._session_id, record.turn_id, content)
            driver = run_turn(
                history,
                record,
                self._complete,
                self._registry,
                self._limits,
                self._on_tool_start,
                self._on_tool_result,
                self._assemble_context if self._context is not None else None,
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

    def new_chat(self) -> str:
        with self._turn_lock:
            if self._active is not None:
                raise RuntimeError("请先结束当前轮次，再开始新聊天。")
            previous_id = self._session_id
            if self._history_path is not None and self._history_path.exists():
                archive = self._history_path.parent / "sessions" / f"session-{previous_id}.json"
                archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                archive.parent.chmod(0o700)
                self._history_path.replace(archive)
            if self._context is not None:
                self._context.archive_task_state(previous_id)
            self._session_id = uuid4().hex
            self._history = []
            self._save_history()
            return self._session_id

    def _advance(self, turn: _ActiveTurn, decision: bool | None = None) -> str | TurnPause:
        try:
            turn.waiting = turn.driver.send(decision)
            return turn.waiting
        except StopIteration as done:
            reply = done.value
            turn.record.messages.append({"role": "assistant", "content": reply})
            self._history.append(turn.record)
            self._save_history()
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
            self._save_history()
        self._active = None

    def _assemble_context(self, messages: list[Message], tools: tuple) -> tuple[list[Message], dict]:
        assert self._context is not None
        result = self._context.assemble(messages, tools)
        trace = result.trace.__dict__
        trace["session_id"] = self._session_id
        trace["_record"] = self._context.record
        return result.messages, trace

    def _save_history(self) -> None:
        if self._history_path is None:
            return
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._history_path.parent.chmod(0o700)
            serialized = []
            for record in self._history:
                messages = []
                for message in record.messages:
                    item = dict(message)
                    calls = item.get("tool_calls")
                    if calls:
                        item["tool_calls"] = [call.__dict__ for call in calls]
                    messages.append(item)
                serialized.append({"turn_id": record.turn_id, "messages": messages})
            temporary = self._history_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"session_id": self._session_id, "turns": serialized}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self._history_path)
        except OSError:
            logging.getLogger(__name__).exception("Unable to persist session history")

    @staticmethod
    def _load_history(path: Path | None) -> tuple[str, list[TurnRecord]]:
        if path is None or not path.exists():
            return uuid4().hex, []
        from src.harness.tools.base import ToolCall
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                session_id, raw_turns = raw.get("session_id") or uuid4().hex, raw["turns"]
            else:
                session_id, raw_turns = uuid4().hex, raw
            records = []
            for record in raw_turns:
                messages = record["messages"]
                for message in messages:
                    if "tool_calls" in message:
                        message["tool_calls"] = tuple(ToolCall(**call) for call in message["tool_calls"])
                records.append(TurnRecord(messages=messages, turn_id=record.get("turn_id") or uuid4().hex))
            return session_id, records
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"无法恢复会话历史 {path}: {error}") from error
