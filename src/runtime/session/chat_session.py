"""Session lifecycle, runtime modes (Normal / Plan / Goal), HITL pauses, and checkpoint resume."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from threading import Lock
from uuid import uuid4

from src.adapters.storage.json_file import atomic_write_json, ensure_private_dir, file_lock
from src.harness.agent_loop.run_turn import (
    ApprovalRequest,
    ClarificationRequest,
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
from src.harness.tools.base import ToolCall, ToolResult
from src.harness.tools.executor import ToolExecutor
from src.harness.tools.registry import ToolRegistry

SESSION_MODES = {"normal", "plan", "goal"}


@dataclass
class _ActiveTurn:
    record: TurnRecord
    driver: TurnDriver
    waiting: TurnPause | None = None
    goal_remaining_rounds: int = 0


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
        checkpoint_path: Path | None = None,
        mode: str = "normal",
        max_goal_rounds: int = 8,
    ) -> None:
        if mode not in SESSION_MODES:
            raise ValueError(f"运行模式必须是 {', '.join(sorted(SESSION_MODES))}。")
        self._complete = complete
        self._registry = registry
        self._limits = limits
        self._on_tool_start = on_tool_start
        self._on_tool_result = on_tool_result
        self._context = context
        self._history_path = history_path
        self._checkpoint_path = (
            checkpoint_path
            if checkpoint_path is not None
            else (history_path.parent / "checkpoint.json" if history_path is not None else None)
        )
        self._approval_policy: Callable[[], str] | None = None
        self._max_goal_rounds = max(1, max_goal_rounds)
        self._session_id, self._history, loaded_mode = self._load_history(history_path)
        self._mode = loaded_mode if (loaded_mode in SESSION_MODES and mode == "normal") else mode
        self._active: _ActiveTurn | None = None
        self._turn_lock = Lock()
        self._restore_checkpoint()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def registry(self) -> ToolRegistry | None:
        return self._registry

    @property
    def has_pending_checkpoint(self) -> bool:
        return self._active is not None and self._active.waiting is not None

    @property
    def pending_pause(self) -> TurnPause | None:
        return self._active.waiting if self._active is not None else None

    def set_mode(self, mode: str) -> str:
        if mode not in SESSION_MODES:
            raise ValueError("运行模式必须是 normal、plan 或 goal。")
        with self._turn_lock:
            self._mode = mode
            self._save_history()
            return self._mode

    def set_model(self, complete: Complete) -> None:
        with self._turn_lock:
            if self._active is not None:
                raise RuntimeError("请先完成当前轮次，或用 /cancel 结束本轮。")
            self._complete = complete

    def get_model(self) -> Complete:
        return self._complete

    def set_tool_observer(self, observer: Callable[[ToolCall, dict], None] | None) -> None:
        with self._turn_lock:
            self._on_tool_start = observer

    def set_tool_result_observer(
        self, observer: Callable[[ToolCall, ToolResult], None] | None,
    ) -> None:
        with self._turn_lock:
            self._on_tool_result = observer

    def set_approval_policy(self, policy: Callable[[], str] | None) -> None:
        """Set a host-owned decision provider: ask, approve, or deny."""
        with self._turn_lock:
            self._approval_policy = policy

    def get_approval_policy(self) -> Callable[[], str] | None:
        return self._approval_policy

    def send(self, content: str) -> str | TurnPause:
        if not content.strip():
            raise ValueError("请输入消息。")
        if not self._turn_lock.acquire(blocking=False):
            raise RuntimeError("请等当前回复结束。")

        try:
            if self._active is not None:
                raise RuntimeError("本轮尚未完成，请继续处理（/resume、/retry 或回答待决问题）或输入 /cancel。")
            goal_rounds = self._max_goal_rounds - 1 if self._mode == "goal" else 0
            return self._start_turn(content, goal_remaining_rounds=goal_rounds)
        finally:
            self._turn_lock.release()

    def answer_approval(self, approved: bool) -> str | TurnPause:
        with self._turn_lock:
            turn = self._active
            if turn is None or not isinstance(turn.waiting, ApprovalRequest):
                raise RuntimeError("当前没有等待审批的操作。")
            was_plan = turn.waiting.kind == "plan"
            outcome = self._advance(turn, approved)
            if was_plan and approved and self._mode == "plan":
                self._mode = "normal"
                self._save_history()
            return outcome

    def answer_clarification(self, answer: str) -> str | TurnPause:
        with self._turn_lock:
            turn = self._active
            if turn is None or not isinstance(turn.waiting, ClarificationRequest):
                raise RuntimeError("当前没有等待回答的澄清问题。")
            return self._advance(turn, answer)

    def retry(self) -> str | TurnPause:
        with self._turn_lock:
            turn = self._active
            if turn is None or not isinstance(turn.waiting, RetryRequest):
                raise RuntimeError("当前没有可以重试的模型请求。")
            return self._advance(turn)

    def resume(self, decision: bool | str | None = None) -> str | TurnPause:
        """Resume a paused turn (including one restored from checkpoint.json across process restarts)."""
        with self._turn_lock:
            turn = self._active
            if turn is None or turn.waiting is None:
                raise RuntimeError("当前没有可恢复的挂起轮次。")
            if isinstance(turn.waiting, ApprovalRequest):
                if not isinstance(decision, bool):
                    raise ValueError("恢复待审批操作需要提供布尔值（True 批准 / False 拒绝）。")
                was_plan = turn.waiting.kind == "plan"
                outcome = self._advance(turn, decision)
                if was_plan and decision and self._mode == "plan":
                    self._mode = "normal"
                    self._save_history()
                return outcome
            if isinstance(turn.waiting, ClarificationRequest):
                if not isinstance(decision, str) or not decision.strip():
                    raise ValueError("恢复待澄清问题需要提供非空回答文本。")
                return self._advance(turn, decision)
            return self._advance(turn, None)

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
                ensure_private_dir(archive.parent)
                self._history_path.replace(archive)
            if self._context is not None:
                self._context.archive_task_state(previous_id)
            self._clear_checkpoint()
            self._session_id = uuid4().hex
            self._history = []
            self._save_history()
            return self._session_id

    def _history_messages(self) -> list[Message]:
        return [
            {**message, "_source_turn_id": record.turn_id}
            for record in self._history
            for message in record.messages
        ]

    def _start_turn(self, content: str, *, goal_remaining_rounds: int = 0) -> str | TurnPause:
        history = self._history_messages()
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
            self._approval_policy,
            mode=self._mode,
        )
        self._active = _ActiveTurn(record, driver, goal_remaining_rounds=goal_remaining_rounds)
        return self._advance(self._active)

    def _advance(self, turn: _ActiveTurn, decision: object = None) -> str | TurnPause:
        try:
            turn.waiting = turn.driver.send(decision)
            self._save_checkpoint(turn)
            return turn.waiting
        except StopIteration as done:
            reply = done.value
            turn.record.messages.append({"role": "assistant", "content": reply})
            self._history.append(turn.record)
            self._save_history()
            self._clear_checkpoint()
            remaining = turn.goal_remaining_rounds
            self._active = None
            if self._mode == "goal" and remaining > 0 and self._should_continue_goal():
                follow_up = self._goal_followup_prompt()
                next_outcome = self._start_turn(follow_up, goal_remaining_rounds=remaining - 1)
                if isinstance(next_outcome, str):
                    return f"{reply}\n\n---\n[Goal 自动推进]\n{next_outcome}"
                return next_outcome
            return reply
        except (Exception, KeyboardInterrupt):
            self._settle_partial(turn)
            raise

    def _should_continue_goal(self) -> bool:
        if self._context is None:
            return False
        state = self._context.task_state.load()
        return state.has_incomplete_steps

    def _goal_followup_prompt(self) -> str:
        assert self._context is not None
        state = self._context.task_state.load()
        pending = [step.title for step in state.plan_steps if step.status != "completed"]
        next_step = pending[0] if pending else "完成最终验证"
        verify_hint = f" 并执行验证命令 `{state.verification_command}`" if state.verification_command else ""
        return (
            f"【Goal 模式自动推进】当前仍有未完成的计划步骤（下一步：{next_step}）。"
            f"请继续执行下一步，完成后调用 update_plan 将对应步骤标记为 completed{verify_hint}。"
        )

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
        self._clear_checkpoint()
        self._active = None

    def _assemble_context(self, messages: list[Message], tools: tuple) -> tuple[list[Message], dict]:
        assert self._context is not None
        result = self._context.assemble(messages, tools, mode=self._mode)
        trace = result.trace.__dict__
        trace["session_id"] = self._session_id
        trace["_record"] = self._context.record
        return result.messages, trace

    def _save_checkpoint(self, turn: _ActiveTurn) -> None:
        if self._checkpoint_path is None or turn.waiting is None:
            return
        try:
            pause = turn.waiting
            if isinstance(pause, ApprovalRequest):
                pause_data = {
                    "type": "approval",
                    "call": pause.call.__dict__,
                    "preview": pause.preview,
                    "kind": pause.kind,
                    "elevated": pause.elevated,
                }
            elif isinstance(pause, ClarificationRequest):
                pause_data = {
                    "type": "clarification",
                    "call": pause.call.__dict__,
                    "question": pause.question,
                    "options": list(pause.options),
                    "allow_custom": pause.allow_custom,
                }
            else:
                pause_data = {"type": "retry", "message": pause.message}

            payload = {
                "session_id": self._session_id,
                "mode": self._mode,
                "turn_id": turn.record.turn_id,
                "model_requests": turn.record.model_requests,
                "goal_remaining_rounds": turn.goal_remaining_rounds,
                "messages": _serialize_messages(turn.record.messages),
                "pause": pause_data,
            }
            with file_lock(self._checkpoint_path):
                atomic_write_json(self._checkpoint_path, payload)
        except OSError:
            logging.getLogger(__name__).exception("Unable to persist turn checkpoint")

    def _clear_checkpoint(self) -> None:
        if self._checkpoint_path is None:
            return
        try:
            with file_lock(self._checkpoint_path):
                if self._checkpoint_path.exists():
                    self._checkpoint_path.unlink()
        except OSError:
            logging.getLogger(__name__).exception("Unable to remove turn checkpoint")

    def _restore_checkpoint(self) -> None:
        if self._checkpoint_path is None or not self._checkpoint_path.exists():
            return
        try:
            raw = json.loads(self._checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            messages = _deserialize_messages(raw.get("messages", []))
            turn_id = str(raw.get("turn_id") or uuid4().hex)
            record = TurnRecord(
                messages=messages,
                turn_id=turn_id,
                model_requests=int(raw.get("model_requests", 0)),
            )
            pause_raw = raw.get("pause", {})
            pause_type = pause_raw.get("type")
            if pause_type == "approval":
                call = ToolCall(**pause_raw["call"])
                waiting: TurnPause = ApprovalRequest(
                    call=call,
                    preview=str(pause_raw.get("preview", "")),
                    kind=str(pause_raw.get("kind", "tool")),
                    elevated=bool(pause_raw.get("elevated", False)),
                )
            elif pause_type == "clarification":
                call = ToolCall(**pause_raw["call"])
                waiting = ClarificationRequest(
                    call=call,
                    question=str(pause_raw.get("question", "")),
                    options=tuple(str(x) for x in pause_raw.get("options", ())),
                    allow_custom=bool(pause_raw.get("allow_custom", True)),
                )
            elif pause_type == "retry":
                waiting = RetryRequest(message=str(pause_raw.get("message", "模型请求中断，可重试。")))
            else:
                return

            goal_rounds = int(raw.get("goal_remaining_rounds", 0))
            saved_mode = str(raw.get("mode", ""))
            if saved_mode in SESSION_MODES:
                self._mode = saved_mode
            driver = self._rehydrated_driver(record, waiting)
            # Prime generator to first yield (which yields `waiting`)
            primed = next(driver)
            self._active = _ActiveTurn(record, driver, waiting=primed, goal_remaining_rounds=goal_rounds)
        except Exception:
            logging.getLogger(__name__).warning("Skipping invalid checkpoint file %s", self._checkpoint_path)

    def _rehydrated_driver(self, record: TurnRecord, waiting: TurnPause) -> TurnDriver:
        """Build a generator that first settles the restored pause and then continues run_turn."""
        decision = yield waiting
        executor = ToolExecutor(self._on_tool_start)
        offered = executor.offered_tools(self._registry.all(mode=self._mode)) if self._registry is not None else ()
        checkpoint = lambda: None

        if isinstance(waiting, ApprovalRequest):
            approved = decision is True
            if not approved:
                outcome = ToolResult(waiting.call.call_id, "用户拒绝了这个操作。请勿原样再次请求。", "APPROVAL_DENIED")
            else:
                step = executor.execute(waiting.call, offered, checkpoint)
                if isinstance(step, ToolResult):
                    outcome = step
                else:
                    outcome = executor.resume(step, True, checkpoint)
            if self._on_tool_result is not None:
                try:
                    self._on_tool_result(waiting.call, outcome)
                except Exception:
                    pass
            record.messages.append({"role": "tool", "content": outcome.model_content(), "call_id": outcome.call_id})
        elif isinstance(waiting, ClarificationRequest):
            cleaned = str(decision or "").strip()
            outcome = (
                ToolResult(waiting.call.call_id, f"用户回答：{cleaned}")
                if cleaned
                else ToolResult(waiting.call.call_id, "用户未提供回答。", "CLARIFICATION_EMPTY")
            )
            if self._on_tool_result is not None:
                try:
                    self._on_tool_result(waiting.call, outcome)
                except Exception:
                    pass
            record.messages.append({"role": "tool", "content": outcome.model_content(), "call_id": outcome.call_id})

        # Ensure any remaining unexecuted tool_calls in the last assistant batch are settled before continuing
        answered = {msg["call_id"] for msg in record.messages if msg["role"] == "tool"}
        last_assistant = next((msg for msg in reversed(record.messages) if msg["role"] == "assistant"), None)
        if last_assistant and last_assistant.get("tool_calls"):
            for call in last_assistant["tool_calls"]:
                if call.call_id in answered:
                    continue
                step = executor.execute(call, offered, checkpoint)
                if isinstance(step, ToolResult):
                    out = step
                else:
                    policy = self._approval_policy() if self._approval_policy is not None else "ask"
                    if hasattr(step, "preview"):
                        if policy == "approve" and not step.elevated and step.kind != "plan":
                            out = executor.resume(step, True, checkpoint)
                        elif policy == "deny":
                            out = executor.resume(step, False, checkpoint)
                        else:
                            sub_pause = ApprovalRequest(step.call, step.preview, kind=step.kind, elevated=step.elevated)
                            sub_decision = yield sub_pause
                            out = executor.resume(step, sub_decision is True, checkpoint)
                    else:
                        if policy == "deny":
                            out = ToolResult(
                                call.call_id,
                                "非交互模式下无法向用户提问，请根据现有上下文作出最稳妥的选择或报告不确定项。",
                                "CLARIFICATION_UNAVAILABLE",
                            )
                        else:
                            sub_pause = ClarificationRequest(step.call, step.question, step.options, step.allow_custom)
                            sub_decision = yield sub_pause
                            out = executor.resume_clarification(step, str(sub_decision or ""))
                if self._on_tool_result is not None:
                    try:
                        self._on_tool_result(call, out)
                    except Exception:
                        pass
                record.messages.append({"role": "tool", "content": out.model_content(), "call_id": out.call_id})

        continuation = run_turn(
            self._history_messages(),
            record,
            self._complete,
            self._registry,
            self._limits,
            self._on_tool_start,
            self._on_tool_result,
            self._assemble_context if self._context is not None else None,
            self._approval_policy,
            mode=self._mode,
        )
        return (yield from continuation)

    def _save_history(self) -> None:
        if self._history_path is None:
            return
        try:
            serialized = [
                {"turn_id": record.turn_id, "messages": _serialize_messages(record.messages)}
                for record in self._history
            ]
            payload = {
                "session_id": self._session_id,
                "mode": self._mode,
                "turns": serialized,
            }
            with file_lock(self._history_path):
                atomic_write_json(self._history_path, payload)
        except OSError:
            logging.getLogger(__name__).exception("Unable to persist session history")

    @staticmethod
    def _load_history(path: Path | None) -> tuple[str, list[TurnRecord], str]:
        if path is None or not path.exists():
            return uuid4().hex, [], "normal"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                session_id = raw.get("session_id") or uuid4().hex
                mode = str(raw.get("mode", "normal"))
                raw_turns = raw["turns"]
            else:
                session_id, mode, raw_turns = uuid4().hex, "normal", raw
            records = []
            for record in raw_turns:
                messages = _deserialize_messages(record["messages"])
                records.append(TurnRecord(messages=messages, turn_id=record.get("turn_id") or uuid4().hex))
            return session_id, records, mode
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f"无法恢复会话历史 {path}: {error}") from error


def _serialize_messages(messages: list[Message]) -> list[dict]:
    serialized = []
    for message in messages:
        item = dict(message)
        calls = item.get("tool_calls")
        if calls:
            item["tool_calls"] = [call.__dict__ for call in calls]
        serialized.append(item)
    return serialized


def _deserialize_messages(raw_messages: list[dict]) -> list[Message]:
    messages: list[Message] = []
    for message in raw_messages:
        item = dict(message)
        if "tool_calls" in item and item["tool_calls"]:
            item["tool_calls"] = tuple(
                call if isinstance(call, ToolCall) else ToolCall(**call)
                for call in item["tool_calls"]
            )
        messages.append(item)  # type: ignore[arg-type]
    return messages
