import json
from unittest import TestCase

from src.harness.agent_loop.run_turn import ModelReply, TurnLimits
from src.harness.tools.base import BaseTool, ToolCall
from src.harness.tools.builtins.bash import _COMMAND_TIMEOUT
from src.harness.tools.registry import ToolRegistry
from src.runtime.session.chat_session import ChatSession


class TurnBudgetTests(TestCase):
    def test_default_turn_budget_allows_longer_tasks_and_multiple_tool_steps(self):
        limits = TurnLimits()

        self.assertGreaterEqual(limits.seconds, 900)
        self.assertGreaterEqual(limits.requests, 40)
        self.assertGreaterEqual(limits.tool_attempts, 80)

    def test_bash_timeout_is_longer_than_one_minute(self):
        self.assertGreaterEqual(_COMMAND_TIMEOUT, 300)

    def test_default_limits_complete_a_task_with_more_than_twelve_tool_steps(self):
        class StepTool(BaseTool):
            name = "step"
            description = "Record one task step."
            parameters = {
                "type": "object",
                "properties": {"number": {"type": "integer"}},
                "required": ["number"],
                "additionalProperties": False,
            }

            def __init__(self):
                self.completed = []

            def execute(self, arguments, checkpoint):
                checkpoint()
                self.completed.append(arguments["number"])
                return f"step {arguments['number']} complete"

        tool = StepTool()
        step = 0

        def complete(messages, tools, timeout):
            nonlocal step
            if step == 13:
                return ModelReply("task complete")
            step += 1
            call = ToolCall(str(step), "step", json.dumps({"number": step}))
            return ModelReply(None, (call,))

        session = ChatSession(complete, ToolRegistry((tool,)))
        self.assertEqual(session.send("Complete all thirteen steps."), "task complete")

        self.assertEqual(tool.completed, list(range(1, 14)))
