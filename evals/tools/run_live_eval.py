"""Run a traceable, isolated end-to-end tool-use evaluation against a live model."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from src.adapters.models.litellm_model import create_model, load_model_config
from src.harness.tools.builtins import create_builtin_registry
from src.harness.agent_loop.run_turn import ApprovalRequest, RetryRequest
from src.harness.tools.base import ToolCall, ToolResult
from src.runtime.session.chat_session import ChatSession


def run(model_alias: str | None = None) -> dict:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(os.environ.get("MINICC_MODELS_CONFIG") or project_root / "models.json")
    config = load_model_config(config_path)
    alias = model_alias or os.environ.get("MINICC_MODEL", "").strip() or config.get("default")
    if not isinstance(alias, str) or not alias:
        raise ValueError("No model alias configured.")

    run_id = str(uuid.uuid4())
    root_span_id = uuid.uuid4().hex[:16]
    run_started_ns = time.time_ns()
    trace = {
        "trace_id": run_id,
        "name": "workspace_status_update",
        "spans": [{
            "span_id": root_span_id,
            "parent_span_id": None,
            "name": "invoke_agent",
            "kind": "agent",
            "trace_id": run_id,
            "gen_ai.conversation.id": run_id,
            "gen_ai.agent.name": "MiniCC.tool_eval",
            "gen_ai.operation.name": "invoke_agent",
            "start_ns": run_started_ns,
            "status": "running",
        }],
        "evaluations": [],
    }
    with TemporaryDirectory(prefix="minicc-tool-eval-") as temporary:
        root = Path(temporary)
        target = root / "README.md"
        target.write_text("status: pending\n", encoding="utf-8")
        real_complete = create_model(config, alias)

        def traced_complete(messages, tools, timeout):
            span = {
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": root_span_id,
                "name": "chat",
                "kind": "llm",
                "trace_id": run_id,
                "gen_ai.conversation.id": run_id,
                "gen_ai.agent.name": "MiniCC.tool_eval",
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": alias,
                "start_ns": time.time_ns(),
                "model": alias,
                "offered_tools": [tool.name for tool in tools],
                "input_message_count": len(messages),
                "timeout_seconds": timeout,
            }
            started = time.monotonic()
            try:
                reply = real_complete(messages, tools, timeout)
                span["tool_call_names"] = [call.name for call in reply.calls]
                span["usage"] = vars(reply.usage) if reply.usage else None
                span["status"] = "ok"
                return reply
            except Exception as error:
                span["status"] = "error"
                span["error_type"] = type(error).__name__
                raise
            finally:
                span["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
                trace["spans"].append(span)

        starts: dict[str, dict] = {}

        def on_tool_start(call: ToolCall, arguments: dict) -> None:
            span = {
                "span_id": uuid.uuid4().hex[:16],
                "parent_span_id": root_span_id,
                "name": "execute_tool",
                "kind": "tool",
                "trace_id": run_id,
                "gen_ai.conversation.id": run_id,
                "gen_ai.agent.name": "MiniCC.tool_eval",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": call.name,
                "gen_ai.tool.call.id": call.call_id,
                "tool_name": call.name,
                "call_id": call.call_id,
                "argument_keys": sorted(arguments),
                "start_ns": time.time_ns(),
                "status": "running",
            }
            starts[call.call_id] = span
            trace["spans"].append(span)

        def on_tool_result(call: ToolCall, result: ToolResult) -> None:
            span = starts.get(call.call_id)
            if span is None:
                span = {
                    "span_id": uuid.uuid4().hex[:16],
                    "parent_span_id": root_span_id,
                    "name": "execute_tool",
                    "kind": "tool",
                    "trace_id": run_id,
                    "gen_ai.conversation.id": run_id,
                    "gen_ai.agent.name": "MiniCC.tool_eval",
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": call.name,
                    "gen_ai.tool.call.id": call.call_id,
                    "tool_name": call.name,
                    "call_id": call.call_id,
                    "status": "running",
                    "start_ns": time.time_ns(),
                }
                trace["spans"].append(span)
            span["status"] = "error" if result.error_code else "ok"
            span["error_code"] = result.error_code
            span["result_chars"] = len(result.content)
            if span["status"] != "running":
                span["duration_ms"] = round((time.time_ns() - span["start_ns"]) / 1_000_000, 2)

        session = ChatSession(
            traced_complete,
            create_builtin_registry(root),
            on_tool_start=on_tool_start,
            on_tool_result=on_tool_result,
        )
        prompt = (
            "请读取 README.md，把 status 从 pending 改成 done。修改前展示差异并等待审批，"
            "获得批准后再读回文件确认。只操作这个文件。"
        )
        task_started = time.monotonic()
        result = session.send(prompt)
        approvals = 0
        retries = 0
        terminal_error = None
        while isinstance(result, (ApprovalRequest, RetryRequest)):
            if isinstance(result, ApprovalRequest):
                approvals += 1
                trace["spans"].append({
                    "span_id": uuid.uuid4().hex[:16],
                    "parent_span_id": root_span_id,
                    "name": "approval",
                    "kind": "human_interaction",
                    "trace_id": run_id,
                    "gen_ai.conversation.id": run_id,
                    "gen_ai.agent.name": "MiniCC.tool_eval",
                    "gen_ai.operation.name": "execute_tool",
                    "decision": "approved_by_eval_harness",
                    "tool_name": result.call.name,
                    "preview_chars": len(result.preview),
                    "start_ns": time.time_ns(),
                    "status": "ok",
                })
                result = session.answer_approval(True)
            else:
                retries += 1
                if retries > 5:
                    terminal_error = result.message
                    result = "Evaluation stopped after repeated model request failures."
                    break
                trace["spans"].append({
                    "span_id": uuid.uuid4().hex[:16],
                    "parent_span_id": root_span_id,
                    "name": "model_retry_wait",
                    "kind": "event",
                    "trace_id": run_id,
                    "gen_ai.conversation.id": run_id,
                    "gen_ai.agent.name": "MiniCC.tool_eval",
                    "gen_ai.operation.name": "chat",
                    "reason": result.message,
                    "status": "retrying",
                })
                result = session.retry()

        elapsed_ms = round((time.monotonic() - task_started) * 1000, 2)
        final_contents = target.read_text(encoding="utf-8")
        llm_spans = [span for span in trace["spans"] if span["kind"] == "llm"]
        tool_spans = [span for span in trace["spans"] if span["kind"] == "tool"]
        usage_spans = [span.get("usage") or {} for span in llm_spans]
        input_tokens = sum(item.get("input_tokens") or 0 for item in usage_spans)
        output_tokens = sum(item.get("output_tokens") or 0 for item in usage_spans)
        actual_write = any(span.get("tool_name") in {"write", "edit"} for span in tool_spans)
        checks = {
            "task_outcome": final_contents == "status: done\n",
            "used_approved_write_tool": actual_write and approvals >= 1,
            "verified_after_write": any(span.get("tool_name") == "read" for span in tool_spans),
            "no_tool_errors": all(span.get("status") == "ok" for span in tool_spans),
            "final_response_present": isinstance(result, str) and bool(result.strip()),
        }
        if terminal_error:
            checks["model_request_recovered"] = False
        elif retries:
            checks["model_request_recovered"] = True
        trace["evaluations"] = [
            {"name": name, "score": int(passed), "passed": passed}
            for name, passed in checks.items()
        ]
        trace["summary"] = {
            "model": alias,
            "passed": all(checks.values()),
            "task_duration_ms": elapsed_ms,
            "llm_calls": len(llm_spans),
            "tool_calls": len(tool_spans),
            "tool_errors": sum(span.get("status") != "ok" for span in tool_spans),
            "approval_count": approvals,
            "retry_count": retries,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": sum(item.get("cached_input_tokens") or 0 for item in usage_spans),
            "tool_schema_bytes_total": sum(item.get("tool_schema_bytes") or 0 for item in usage_spans),
            "final_file": final_contents,
            "final_answer": result,
            "terminal_error": terminal_error,
        }
        trace["spans"][0].update({
            "status": "ok" if all(checks.values()) else "error",
            "duration_ms": elapsed_ms,
            "minicc.eval.passed": all(checks.values()),
            "minicc.eval.checks": checks,
            "minicc.eval.model": alias,
        })
    return trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Configured model alias; defaults to MINICC_MODEL or models.json default.")
    parser.add_argument("--output", type=Path, help="Write the trace JSON to this path instead of stdout.")
    args = parser.parse_args()
    trace = run(args.model)
    serialized = json.dumps(trace, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0 if trace["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
