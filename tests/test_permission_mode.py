import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from src.client.terminal_chat import PermissionModeController, show_result
from src.harness.agent_loop.run_turn import ApprovalRequest, ModelReply
from src.harness.permission.workspace import Workspace
from src.harness.tools.base import ToolCall
from src.harness.tools.builtins.write import WriteTool
from src.harness.tools.registry import ToolRegistry
from src.runtime.session.chat_session import ChatSession


class PermissionModeTests(TestCase):
    def test_default_mode_asks_before_writing_and_auto_mode_skips_the_prompt(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "result.txt"
            replies = iter((
                ModelReply(None, (ToolCall("write", "write", json.dumps({
                    "path": "result.txt", "content": "done\n",
                })),)),
                ModelReply("completed"),
            ))
            mode = PermissionModeController()
            session = ChatSession(
                lambda messages, tools, timeout: next(replies),
                ToolRegistry((WriteTool(Workspace(root)),)),
            )
            session.set_approval_policy(mode.approval_policy)

            self.assertEqual(mode.mode, "default")
            mode.set("auto")
            self.assertEqual(session.send("Create the result file."), "completed")

            self.assertEqual(target.read_text(encoding="utf-8"), "done\n")

    def test_auto_choice_approves_current_and_later_tools_in_the_turn(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            replies = iter((
                ModelReply(None, (ToolCall("write-1", "write", json.dumps({
                    "path": "first.txt", "content": "first\n",
                })),)),
                ModelReply(None, (ToolCall("write-2", "write", json.dumps({
                    "path": "second.txt", "content": "second\n",
                })),)),
                ModelReply("completed"),
            ))
            mode = PermissionModeController()
            session = ChatSession(
                lambda messages, tools, timeout: next(replies),
                ToolRegistry((WriteTool(Workspace(root)),)),
            )
            session.set_approval_policy(mode.approval_policy)
            pending = session.send("Create two files.")
            self.assertIsInstance(pending, ApprovalRequest)

            with redirect_stdout(io.StringIO()):
                show_result(session, pending, permission_mode=mode, read_input=lambda _prompt: "a")

            self.assertTrue(mode.auto_approve)
            self.assertEqual((root / "first.txt").read_text(encoding="utf-8"), "first\n")
            self.assertEqual((root / "second.txt").read_text(encoding="utf-8"), "second\n")
