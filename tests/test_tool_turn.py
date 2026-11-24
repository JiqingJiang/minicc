import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from litellm import ModelResponse

from src.adapters.models.litellm_model import create_model
from src.harness.tools.builtins import create_builtin_registry
from src.harness.agent_loop.run_turn import ApprovalRequest, ModelReply, ModelRequestError, RetryRequest, TurnLimits
from src.harness.tools.executor import PendingApproval, ToolExecutor
from src.harness.permission.workspace import Workspace
from src.harness.tools.builtins.bash import BashTool
from src.harness.tools.builtins.grep import GrepTool
from src.harness.tools.builtins.tool_search import ToolSearchTool
from src.harness.tools.registry import ToolRegistry
from src.harness.tools.base import BaseTool, ToolCall, ToolFailure, ToolResult, schema_size
from src.runtime.session.chat_session import ChatSession


def call_result(call_id: str, name: str, **arguments) -> ModelReply:
    return ModelReply(None, (ToolCall(call_id, name, json.dumps(arguments)),))


def scripted_session(root: Path, *replies, limits: TurnLimits = TurnLimits()):
    responses = iter(replies)
    requests = []

    def complete(messages, offered, timeout):
        requests.append((messages, tuple(tool.name for tool in offered)))
        reply = next(responses)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return ChatSession(complete, create_builtin_registry(root), limits), requests


class ToolTurnTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file = self.root / "README.md"
        self.file.write_text("status: pending\n", encoding="utf-8")

    def test_retry_reuses_committed_result(self):
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelRequestError("暂时不可用"),
            call_result("verify", "read", path="README.md"),
            ModelReply("verified"),
            ModelReply("next turn"),
        )
        pending = session.send("update status")
        self.assertIsInstance(pending, ApprovalRequest)
        self.assertIn("pending", pending.preview)
        self.assertIn("done", pending.preview)
        self.assertIn("edit", requests[0][1])
        self.assertEqual(self.file.read_text(), "status: pending\n")
        self.assertIsInstance(session.answer_approval(True), RetryRequest)
        self.assertEqual(requests[0][1], requests[1][1])
        self.assertEqual(requests[1][0][-1]["call_id"], "patch")
        self.assertIn("Updated README.md", requests[1][0][-1]["content"])
        self.assertEqual(self.file.read_text(), "status: done\n")
        self.assertEqual(session.retry(), "verified")
        self.assertIn("done", requests[3][0][-1]["content"])
        self.assertEqual(session.send("continue"), "next turn")
        next_request = requests[-1][0]
        self.assertEqual(
            [message["role"] for message in next_request],
            ["system", "user", "assistant", "tool", "assistant", "tool", "assistant", "user"],
        )
        self.assertEqual(
            [message["call_id"] for message in next_request if message["role"] == "tool"],
            ["patch", "verify"],
        )

    def test_tool_enabled_model_requests_include_approval_policy(self):
        session, requests = scripted_session(self.root, ModelReply("ready"))

        self.assertEqual(session.send("write a file"), "ready")
        system = requests[0][0][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("read、write、edit、tool_search、bash、glob、grep", system["content"])
        self.assertIn("glob、grep", system["content"])

    def test_rejection_then_repeated_patch_never_executes(self):
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            call_result("repeat", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelReply("declined"),
        )
        self.assertIsInstance(session.send("update status"), ApprovalRequest)
        self.assertEqual(session.answer_approval(False), "declined")
        self.assertIn("APPROVAL_DENIED", requests[2][0][-1]["content"])
        self.assertEqual(self.file.read_text(), "status: pending\n")

    def test_patch_rechecks_the_preview_baseline(self):
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelReply("conflict"),
        )
        self.assertIsInstance(session.send("update status"), ApprovalRequest)
        self.file.write_text("status: pending\nchanged externally\n", encoding="utf-8")
        self.assertEqual(session.answer_approval(True), "conflict")
        self.assertTrue(requests[-1][0][-1]["content"].startswith("["))
        self.assertEqual(self.file.read_text(), "status: pending\nchanged externally\n")

    def test_patch_preserves_change_racing_with_atomic_publish(self):
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelReply("conflict"),
        )
        self.assertIsInstance(session.send("update status"), ApprovalRequest)

        from src.harness.permission import workspace as workspace_module

        exchange = workspace_module._exchange

        def race_before_exchange(parent, left, right):
            self.file.write_text("status: changed concurrently\n", encoding="utf-8")
            return exchange(parent, left, right)

        with patch("src.harness.permission.workspace._exchange", side_effect=race_before_exchange):
            self.assertEqual(session.answer_approval(True), "conflict")

        self.assertEqual(self.file.read_text(), "status: changed concurrently\n")
        self.assertIn("FILE_CHANGED", requests[-1][0][-1]["content"])

    def test_patch_keeps_conflicting_copy_if_atomic_rollback_fails(self):
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelReply("conflict"),
        )
        self.assertIsInstance(session.send("update status"), ApprovalRequest)

        from src.harness.permission import workspace as workspace_module

        exchange = workspace_module._exchange
        calls = 0

        def race_and_fail_rollback(parent, left, right):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.file.write_text("status: changed concurrently\n", encoding="utf-8")
                return exchange(parent, left, right)
            raise OSError("simulated rollback failure")

        with patch("src.harness.permission.workspace._exchange", side_effect=race_and_fail_rollback):
            self.assertEqual(session.answer_approval(True), "conflict")

        preserved = list(self.root.glob(".minicc-*.tmp"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(encoding="utf-8"), "status: changed concurrently\n")
        self.assertIn(preserved[0].name, requests[-1][0][-1]["content"])

    def test_patch_preserves_change_racing_with_conflict_rollback(self):
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelReply("conflict"),
        )
        self.assertIsInstance(session.send("update status"), ApprovalRequest)

        from src.harness.permission import workspace as workspace_module

        exchange = workspace_module._exchange
        calls = 0

        def race_on_both_sides_of_exchange(parent, left, right):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.file.write_text("status: changed concurrently\n", encoding="utf-8")
            elif calls == 2:
                self.file.write_text("status: later concurrent edit\n", encoding="utf-8")
            return exchange(parent, left, right)

        with patch("src.harness.permission.workspace._exchange", side_effect=race_on_both_sides_of_exchange):
            self.assertEqual(session.answer_approval(True), "conflict")

        self.assertEqual(self.file.read_text(encoding="utf-8"), "status: changed concurrently\n")
        preserved = list(self.root.glob(".minicc-*.tmp"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(encoding="utf-8"), "status: later concurrent edit\n")
        self.assertIn(preserved[0].name, requests[-1][0][-1]["content"])

    def test_approval_cannot_follow_a_replaced_path_outside_the_workspace(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        outside = Path(directory.name) / "README.md"
        outside.write_text("status: pending\n", encoding="utf-8")
        session, requests = scripted_session(
            self.root,
            call_result("patch", "edit", path="README.md", old_text="pending", new_text="done"),
            ModelReply("blocked"),
        )
        self.assertIsInstance(session.send("update status"), ApprovalRequest)
        self.file.unlink()
        self.file.symlink_to(outside)
        self.assertEqual(session.answer_approval(True), "blocked")
        self.assertTrue(requests[-1][0][-1]["content"].startswith("["))
        self.assertTrue(self.file.is_symlink())
        self.assertEqual(outside.read_text(), "status: pending\n")

    def test_tool_errors_keep_call_ids_and_budget_disables_tools(self):
        session, requests = scripted_session(
            self.root,
            ModelReply(None, (
                ToolCall("bad-json", "read", '{"path":"README.md","path":"other"}'),
                ToolCall("unknown", "unknown_tool", '{}'),
                ToolCall("over-budget", "read", '{"path":"README.md"}'),
            )),
            ModelReply("stopped"),
            limits=TurnLimits(requests=4, tool_attempts=2),
        )
        self.assertEqual(session.send("work"), "stopped")
        self.assertFalse(requests[1][1])
        results = [message for message in requests[1][0] if message["role"] == "tool"]
        self.assertEqual([message["call_id"] for message in results], ["bad-json", "unknown", "over-budget"])
        self.assertTrue(all(message["content"].startswith("[") for message in results))
        self.assertIn("TOOL_BUDGET", results[-1]["content"])
        self.assertEqual(self.file.read_text(), "status: pending\n")

    def test_tool_result_observer_receives_success_and_failure(self):
        events = []
        responses = iter((
            call_result("bad", "read", path="../outside.txt"),
            call_result("good", "read", path="README.md"),
            ModelReply("recovered"),
        ))
        session = ChatSession(
            lambda messages, offered, timeout: next(responses),
            create_builtin_registry(self.root),
            on_tool_result=lambda call, result: events.append((call.name, result.error_code, result.content)),
        )

        self.assertEqual(session.send("read the file"), "recovered")
        self.assertEqual(events[0][0:2], ("read", "INVALID_PATH"))
        self.assertIn("status: pending", events[1][2])
        self.assertEqual(events[1][0:2], ("read", None))

    def test_executor_keeps_the_core_tools_within_the_schema_budget(self):
        registry = create_builtin_registry(self.root)
        visible = ToolExecutor().offered_tools(registry.all())
        self.assertEqual(
            [tool.name for tool in registry.all()],
            ["read", "write", "edit", "tool_search", "bash", "glob", "grep", "web_search", "web_fetch"],
        )
        self.assertEqual(visible, registry.all())
        self.assertLessEqual(schema_size(visible), 3_600)

    def test_write_creates_only_after_approval(self):
        target = self.root / "new.txt"
        session, _ = scripted_session(
            self.root, call_result("write", "write", path="new.txt", content="new\n"), ModelReply("done"),
        )
        pending = session.send("write a file")
        self.assertIsInstance(pending, ApprovalRequest)
        self.assertIn("new", pending.preview)
        self.assertFalse(target.exists())
        self.assertEqual(session.answer_approval(True), "done")
        self.assertEqual(target.read_text(), "new\n")

    def test_write_does_not_overwrite_concurrent_creation(self):
        target = self.root / "new.txt"
        session, requests = scripted_session(
            self.root, call_result("write", "write", path="new.txt", content="new\n"), ModelReply("done"),
        )
        self.assertIsInstance(session.send("write a file"), ApprovalRequest)
        target.write_text("existing\n")
        self.assertEqual(session.answer_approval(True), "done")
        self.assertEqual(target.read_text(), "existing\n")
        self.assertTrue(requests[-1][0][-1]["content"].startswith("["))

    def test_write_existing_file_uses_the_approved_preview(self):
        session, _ = scripted_session(
            self.root, call_result("write", "write", path="README.md", content="overwritten\n"), ModelReply("done"),
        )
        pending = session.send("write a file")
        self.assertIsInstance(pending, ApprovalRequest)
        self.assertIn("pending", pending.preview)
        self.assertEqual(session.answer_approval(False), "done")
        self.assertEqual(self.file.read_text(), "status: pending\n")

    def test_bash_runs_only_after_approval_and_can_delete_files(self):
        target = self.root / "remove-me.txt"
        target.write_text("temporary", encoding="utf-8")
        replies = iter((
            call_result("delete", "bash", command="rm -- remove-me.txt && printf deleted"),
            ModelReply("deleted"),
        ))
        session = ChatSession(lambda messages, offered, timeout: next(replies), ToolRegistry((BashTool(self.root),)))

        pending = session.send("delete the temporary file")
        self.assertIsInstance(pending, ApprovalRequest)
        self.assertIn("rm -- remove-me.txt", pending.preview)
        self.assertTrue(target.exists())
        self.assertEqual(session.answer_approval(True), "deleted")
        self.assertFalse(target.exists())

    def test_rejected_bash_command_does_not_execute(self):
        target = self.root / "must-stay.txt"
        target.write_text("safe", encoding="utf-8")
        replies = iter((
            call_result("delete", "bash", command="rm -- must-stay.txt"),
            ModelReply("kept"),
        ))
        session = ChatSession(lambda messages, offered, timeout: next(replies), ToolRegistry((BashTool(self.root),)))

        self.assertIsInstance(session.send("delete it"), ApprovalRequest)
        self.assertEqual(session.answer_approval(False), "kept")
        self.assertEqual(target.read_text(encoding="utf-8"), "safe")

    def test_repeated_reads_are_blocked(self):
        session, requests = scripted_session(
            self.root,
            *(call_result(str(index), "read", path="README.md") for index in range(4)),
            ModelReply("done"),
        )
        self.assertEqual(session.send("read it"), "done")
        self.assertIn("多次得到相同结果", requests[3][0][-1]["content"])
        self.assertIn("REPEATED_CALL", requests[4][0][-1]["content"])

    def test_adapter_pairs_wire_calls_and_results(self):
        tool_call = ModelResponse(choices=[{
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_read",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"README.md"}'},
            }]},
        }])
        answer = ModelResponse(choices=[{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "verified"},
        }])
        config = {"models": {"test": {"model": "deepseek/deepseek-flash", "api_key_env": "TEST_MODEL_KEY"}}}
        with patch.dict("os.environ", {"TEST_MODEL_KEY": "fixture"}):
            with patch("litellm.completion", side_effect=[tool_call, answer]) as completion:
                session = ChatSession(create_model(config, "test"), create_builtin_registry(self.root))
                self.assertEqual(session.send("read it"), "verified")
        initial = completion.call_args_list[0].kwargs
        self.assertTrue(initial["tools"])
        assistant, result = completion.call_args_list[1].kwargs["messages"][-2:]
        self.assertEqual(assistant["tool_calls"][0]["id"], result["tool_call_id"])
        self.assertIn("status: pending", result["content"])


class ToolExecutorTests(TestCase):
    def test_grep_stops_a_catastrophic_regular_expression(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "long-line.txt").write_text("a" * 50_000 + "!\n", encoding="utf-8")
            tool = GrepTool(Workspace(root))

            with self.assertRaisesRegex(ToolFailure, "正则表达式匹配超时"):
                tool.execute({"pattern": "(a+)+$", "path": "long-line.txt"}, lambda: None)

    def test_tool_search_omits_invalid_tools_that_are_not_offered(self):
        class Invalid(BaseTool):
            name = "secret_lookup"
            description = "Look up secret values."
            parameters = {"type": "object", "properties": {"x": {"$ref": "https://invalid"}}}

            def execute(self, arguments, checkpoint):
                return "never"

        registry = ToolRegistry()
        registry.register(Invalid())
        registry.register(ToolSearchTool(registry))
        result = ToolSearchTool(registry).execute({"query": "secret lookup"}, lambda: None)
        self.assertNotIn("secret_lookup", result)

    def test_fingerprint_uses_executor_strict_json_rules(self):
        from src.harness.tools.executor import fingerprint_arguments

        self.assertEqual(fingerprint_arguments('{"x":1, "y":2}'), fingerprint_arguments('{"y":2,"x":1}'))
        self.assertNotEqual(fingerprint_arguments('{"x":1,"x":2}'), fingerprint_arguments('{"x":2}'))
        self.assertEqual(fingerprint_arguments('{"x":NaN}'), '{"x":NaN}')

    def test_schema_format_is_enforced(self):
        class UriTool(BaseTool):
            name = "ipv4_check"
            description = "Validate an IPv4 address."
            parameters = {
                "type": "object",
                "properties": {"ip": {"type": "string", "format": "ipv4"}},
                "required": ["ip"],
            }

            def execute(self, arguments, checkpoint):
                return "ok"

        result = ToolExecutor().execute(
            ToolCall("bad-ip", "ipv4_check", '{"ip":"999.999.999.999"}'), (UriTool(),), lambda: None,
        )
        self.assertEqual(result.error_code, "INVALID_ARGUMENTS")

    def test_approval_preview_limit_is_measured_in_utf8_bytes(self):
        class Approved(BaseTool):
            name = "approved"
            description = "Requires approval."
            parameters = {"type": "object", "properties": {}, "additionalProperties": False}
            requires_approval = True

            def execute(self, arguments, checkpoint):
                return "done"

            def approval(self, arguments, checkpoint):
                class Action:
                    preview = "界" * 3_000

                    def execute(self, checkpoint):
                        return "done"
                return Action()

        result = ToolExecutor().execute(ToolCall("preview", "approved", "{}"), (Approved(),), lambda: None)
        self.assertEqual(result.error_code, "APPROVAL_PREVIEW_INVALID")

    def test_tool_start_observer_receives_validated_arguments_at_execution(self):
        class Echo(BaseTool):
            name = "echo"
            description = "Echo a string."
            parameters = {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            }

            def execute(self, arguments, checkpoint):
                return arguments["text"]

        events = []
        tool = Echo()
        executor = ToolExecutor(lambda call, arguments: events.append((call.name, arguments)))
        offered = (tool,)
        invalid = executor.execute(ToolCall("bad", "echo", '{"text":2}'), offered, lambda: None)
        self.assertEqual(invalid.error_code, "INVALID_ARGUMENTS")
        self.assertEqual(events, [])
        result = executor.execute(ToolCall("ok", "echo", '{"text":"你好"}'), offered, lambda: None)
        self.assertEqual(result.content, "你好")
        self.assertEqual(events, [("echo", {"text": "你好"})])

    def test_registering_a_tool_instance_needs_only_its_class(self):
        class Echo(BaseTool):
            name = "echo"
            description = "Echo a string."
            parameters = {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            }

            def execute(self, arguments, checkpoint):
                self.calls += 1
                if arguments["text"] == "broken":
                    raise RuntimeError("private traceback detail")
                return arguments["text"]

        tool = Echo()
        tool.calls = 0
        registry = ToolRegistry()
        registry.register(tool)
        offered = registry.all()
        executor = ToolExecutor()
        checkpoint = lambda: None
        invalid = executor.execute(ToolCall("1", "echo", '{"text": 42}'), offered, checkpoint)
        self.assertEqual(invalid.error_code, "INVALID_ARGUMENTS")
        self.assertEqual(tool.calls, 0)
        result = executor.execute(ToolCall("2", "echo", '{"text": "ok"}'), offered, checkpoint)
        self.assertEqual(result, ToolResult("2", "ok"))
        failed = executor.execute(ToolCall("3", "echo", '{"text": "broken"}'), offered, checkpoint)
        self.assertEqual(failed.error_code, "TOOL_ERROR")
        self.assertNotIn("private traceback detail", failed.content)

        tool.requires_approval = True
        pending = executor.execute(ToolCall("4", "echo", '{"text": "approved"}'), offered, checkpoint)
        self.assertIsInstance(pending, PendingApproval)
        self.assertEqual(tool.calls, 2)
        self.assertEqual(executor.resume(pending, True, checkpoint), ToolResult("4", "approved"))
        self.assertEqual(executor.resume(pending, True, checkpoint).error_code, "APPROVAL_RESOLVED")
        self.assertEqual(tool.calls, 3)

    def test_registry_accepts_tools_and_executor_owns_schema_validation(self):
        class Invalid(BaseTool):
            name = "invalid"
            description = "Invalid schema fixture."
            parameters = {"type": "object", "properties": {"path": {"$ref": "https://example.invalid/schema"}}}
            ran = False

            def execute(self, arguments, checkpoint):
                self.ran = True
                return "unexpected execution"

        tool = Invalid()
        registry = ToolRegistry((tool,))
        self.assertIs(registry.get("invalid"), tool)
        executor = ToolExecutor()
        self.assertEqual(executor.offered_tools(registry.all()), ())
        result = executor.execute(ToolCall("invalid-schema", "invalid", '{}'), registry.all(), lambda: None)
        self.assertEqual(result.call_id, "invalid-schema")
        self.assertIsNotNone(result.error_code)
        self.assertFalse(tool.ran)
