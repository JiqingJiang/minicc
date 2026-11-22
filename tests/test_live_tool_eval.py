import json
from unittest import TestCase
from unittest.mock import patch

from evals.tools.run_live_eval import run
from src.harness.agent_loop.run_turn import ModelReply
from src.harness.tools.base import ToolCall


class LiveEvalHarnessTests(TestCase):
    def test_scripted_golden_path_emits_hierarchical_trace_and_passes_graders(self):
        replies = iter((
            ModelReply(None, (ToolCall("read", "read", '{"path":"README.md"}'),)),
            ModelReply(None, (ToolCall(
                "patch", "edit",
                json.dumps({"path": "README.md", "old_text": "pending", "new_text": "done"}),
            ),)),
            ModelReply(None, (ToolCall("verify", "read", '{"path":"README.md"}'),)),
            ModelReply("Updated and verified."),
        ))

        with (
            patch("evals.tools.run_live_eval.load_model_config", return_value={"default": "fixture", "models": {"fixture": {}}}),
            patch("evals.tools.run_live_eval.create_model", return_value=lambda messages, tools, timeout: next(replies)),
        ):
            trace = run()

        self.assertTrue(trace["summary"]["passed"])
        self.assertEqual(trace["summary"]["final_file"], "status: done\n")
        self.assertEqual(trace["summary"]["approval_count"], 1)
        self.assertEqual(trace["summary"]["tool_calls"], 3)
        root = trace["spans"][0]
        self.assertTrue(all(span["parent_span_id"] == root["span_id"] for span in trace["spans"][1:]))
