import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.client.terminal_chat import run_terminal_chat, show_result
from src.harness.agent_loop.run_turn import ApprovalRequest, ModelReply
from src.harness.permission.workspace import Workspace
from src.harness.tools.builtins.write import WriteTool
from src.harness.tools.registry import ToolRegistry
from src.harness.tools.base import ToolCall
from src.runtime.session.chat_session import ChatSession


class TerminalApprovalTests(TestCase):
    def test_yes_choice_executes_real_pending_approval(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            replies = iter((
                ModelReply(None, (ToolCall("write", "write", '{"path":"HITL_TEST.txt","content":"approved\\n"}'),)),
                ModelReply("File created and verified."),
            ))
            session = ChatSession(lambda messages, tools, timeout: next(replies), ToolRegistry((WriteTool(Workspace(root)),)))
            pending = session.send("Create the test file.")
            self.assertIsInstance(pending, ApprovalRequest)
            self.assertFalse((root / "HITL_TEST.txt").exists())

            output = io.StringIO()
            def choose_yes(prompt):
                print(prompt, end="")
                return "yes"

            with patch("builtins.input", side_effect=choose_yes), redirect_stdout(output):
                show_result(session, pending)

            self.assertEqual((root / "HITL_TEST.txt").read_text(encoding="utf-8"), "approved\n")
            self.assertIn("工具审批：请作出选择", output.getvalue())
            self.assertIn("yes 批准 / no 拒绝", output.getvalue())

    def test_no_approval_request_rejects_bare_yes_no_as_chat(self):
        model_requests = []
        session = ChatSession(
            lambda messages, tools, timeout: (model_requests.append(messages) or ModelReply("received")),
        )
        errors = io.StringIO()
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["n", "hello", EOFError]),
            redirect_stderr(errors),
            redirect_stdout(output),
        ):
            run_terminal_chat(
                session,
                active_model="fixture",
                models={"fixture": "Fixture model"},
                select_model=lambda alias: None,
            )

        self.assertEqual(len(model_requests), 1)
        self.assertEqual(model_requests[0][-1]["content"], "hello")
        self.assertIn("当前没有待处理的工具审批", errors.getvalue())
