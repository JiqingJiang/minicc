"""Comprehensive tests for Plan/Goal modes, HITL ask_user, Persistence & Checkpoint Resume, SubAgents, MCP, Sandbox, and Memory cross-category correction."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

from src.adapters.mcp.client import McpManager, McpServerConfig, McpStdioClient
from src.adapters.sandbox.executor import SandboxPolicy, build_seatbelt_profile, sandbox_available
from src.harness.agent_loop.run_turn import (
    ApprovalRequest,
    ClarificationRequest,
    ModelReply,
    ModelRequestError,
    RequestUsage,
    RetryRequest,
)
from src.harness.context.assembler import ContextAssembler
from src.harness.memory.manager import MemoryManager
from src.harness.memory.remember_tool import RememberMemoryTool
from src.harness.orchestration.subagents.runner import DelegateSubAgentTool, SubAgentRunner
from src.harness.tools.base import ToolCall
from src.harness.tools.builtins import create_builtin_registry
from src.harness.tools.builtins.bash import BashTool
from src.harness.tools.registry import ToolRegistry
from src.runtime.hitl.contracts import AskUserTool, UpdatePlanTool
from src.runtime.session.chat_session import ChatSession
from src.runtime.task.state import TaskStateStore
from src.client.terminal_chat import _manage_mcp, _manage_memory, _manage_skills, _manage_tools
from src.harness.skills import SkillRegistry
from contextlib import redirect_stdout
import io


def call_reply(call_id: str, name: str, **kwargs) -> ModelReply:
    return ModelReply(
        None,
        (ToolCall(call_id, name, json.dumps(kwargs, ensure_ascii=False)),),
        usage=RequestUsage("test-model", 10, 5, 0, 100),
    )


class RefactorHarnessTests(TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")

    def _build_full_registry(self, task_store: TaskStateStore) -> ToolRegistry:
        registry = create_builtin_registry(self.root)
        registry.register(AskUserTool())
        registry.register(UpdatePlanTool(task_store))
        return registry

    def test_write_creates_missing_nested_subdirectories_safely(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        registry = self._build_full_registry(task_store)
        replies = iter((
            call_reply("w1", "write", path="src/nested/pkg/mod.py", content="x = 42\n"),
            ModelReply("created nested file"),
        ))
        session = ChatSession(lambda m, t, to: next(replies), registry)
        session.set_approval_policy(lambda: "approve")
        self.assertEqual(session.send("create nested module"), "created nested file")
        self.assertEqual((self.root / "src/nested/pkg/mod.py").read_text(encoding="utf-8"), "x = 42\n")

    def test_same_bash_test_command_can_run_before_and_after_code_edit(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        registry = self._build_full_registry(task_store)
        replies = iter((
            call_reply("test-1", "bash", command="python3 -B -c 'import app; print(app.value)'"),
            call_reply("edit-1", "edit", path="app.py", old_text="value = 1", new_text="value = 2"),
            call_reply("test-2", "bash", command="python3 -B -c 'import app; print(app.value)'"),
            ModelReply("Test passed with value = 2"),
        ))
        results: list[tuple[str, str | None, str]] = []
        session = ChatSession(
            lambda m, t, to: next(replies),
            registry,
            on_tool_result=lambda c, r: results.append((c.call_id, r.error_code, r.content.strip())),
        )
        session.set_approval_policy(lambda: "approve")

        answer = session.send("run test, update value, and re-run test")
        self.assertEqual(answer, "Test passed with value = 2")
        self.assertEqual(results[0], ("test-1", None, "1"))
        self.assertEqual(results[1][0:2], ("edit-1", None))
        self.assertEqual(results[2], ("test-2", None, "2"))

    def test_plan_mode_blocks_write_tools_and_transitions_on_plan_approval(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        registry = self._build_full_registry(task_store)
        offered_in_requests: list[tuple[str, ...]] = []
        replies = iter((
            call_reply("r1", "read", path="app.py"),
            call_reply(
                "p1",
                "update_plan",
                goal="Upgrade value to 2",
                steps=[
                    {"title": "Edit app.py", "status": "pending"},
                    {"title": "Verify app.py", "status": "pending"},
                ],
                verification_command="python3 -c 'import app'",
                submit_for_approval=True,
            ),
            ModelReply("Plan approved and ready for execution."),
        ))

        def complete(messages, offered, timeout):
            offered_in_requests.append(tuple(tool.name for tool in offered))
            return next(replies)

        session = ChatSession(complete, registry, mode="plan")
        pause = session.send("Plan the upgrade")
        self.assertIsInstance(pause, ApprovalRequest)
        self.assertEqual(pause.kind, "plan")
        # In plan mode, write/edit/bash must NOT be offered
        self.assertNotIn("write", offered_in_requests[0])
        self.assertNotIn("edit", offered_in_requests[0])
        self.assertNotIn("bash", offered_in_requests[0])
        self.assertIn("read", offered_in_requests[0])
        self.assertIn("update_plan", offered_in_requests[0])

        reply = session.answer_approval(True)
        self.assertEqual(reply, "Plan approved and ready for execution.")
        # After approving plan, session automatically transitions to normal mode
        self.assertEqual(session.mode, "normal")
        state = task_store.load()
        self.assertTrue(state.plan_approved)
        self.assertEqual(len(state.plan_steps), 2)

    def test_goal_mode_autonomously_iterates_until_all_plan_steps_completed(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        registry = self._build_full_registry(task_store)
        memory = MemoryManager(self.state_dir, "proj")
        context = ContextAssembler(self.root, memory=memory, task_store=task_store, trace_path=self.state_dir / "trace.jsonl")

        replies = iter((
            # Round 1: sets plan with 2 steps, completes step 1
            call_reply(
                "p1",
                "update_plan",
                goal="Two-step refactor",
                steps=[
                    {"title": "Step 1: edit app.py", "status": "completed"},
                    {"title": "Step 2: verify app.py", "status": "pending"},
                ],
            ),
            ModelReply("Completed step 1."),
            # Round 2 (triggered automatically by Goal mode): completes step 2
            call_reply(
                "p2",
                "update_plan",
                goal="Two-step refactor",
                steps=[
                    {"title": "Step 1: edit app.py", "status": "completed"},
                    {"title": "Step 2: verify app.py", "status": "completed"},
                ],
            ),
            ModelReply("Completed step 2 and verified."),
        ))

        session = ChatSession(
            lambda m, t, to: next(replies),
            registry,
            context=context,
            mode="goal",
            max_goal_rounds=4,
        )
        session.set_approval_policy(lambda: "approve")
        final_output = session.send("Run the two-step refactor to completion")
        self.assertIn("Completed step 1.", final_output)
        self.assertIn("[Goal 自动推进]", final_output)
        self.assertIn("Completed step 2 and verified.", final_output)
        self.assertFalse(task_store.load().has_incomplete_steps)

    def test_ask_user_hitl_pauses_and_returns_clarification_answer(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        registry = self._build_full_registry(task_store)
        captured_messages = []
        replies = iter((
            call_reply(
                "ask-1",
                "ask_user",
                question="Which output format do you prefer?",
                options=["JSON", "YAML"],
            ),
            ModelReply("Proceeding with YAML."),
        ))

        def complete(messages, offered, timeout):
            captured_messages.append(list(messages))
            return next(replies)

        session = ChatSession(complete, registry)
        pause = session.send("Export the config")
        self.assertIsInstance(pause, ClarificationRequest)
        self.assertEqual(pause.options, ("JSON", "YAML"))
        reply = session.answer_clarification("YAML")
        self.assertEqual(reply, "Proceeding with YAML.")
        self.assertIn("用户回答：YAML", captured_messages[-1][-1]["content"])

    def test_persistence_and_cross_process_checkpoint_resume(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        history_path = self.state_dir / "session.json"
        checkpoint_path = self.state_dir / "checkpoint.json"

        # Process 1: reads app.py, then requests approval to edit app.py, then process exits
        registry1 = self._build_full_registry(task_store)
        replies1 = iter((
            call_reply("read-1", "read", path="app.py"),
            call_reply("edit-1", "edit", path="app.py", old_text="value = 1", new_text="value = 99"),
        ))
        session1 = ChatSession(
            lambda m, t, to: next(replies1),
            registry1,
            history_path=history_path,
            checkpoint_path=checkpoint_path,
        )
        pause = session1.send("Update value to 99")
        self.assertIsInstance(pause, ApprovalRequest)
        self.assertTrue(checkpoint_path.exists())
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "value = 1\n")

        # Simulate process restart: create a brand-new ChatSession pointing to same files
        registry2 = self._build_full_registry(task_store)
        captured_after_resume = []
        replies2 = iter((
            ModelReply("Resumed across process restart and updated value to 99."),
        ))

        def complete2(messages, offered, timeout):
            captured_after_resume.append(list(messages))
            return next(replies2)

        session2 = ChatSession(
            complete2,
            registry2,
            history_path=history_path,
            checkpoint_path=checkpoint_path,
        )
        self.assertTrue(session2.has_pending_checkpoint)
        self.assertIsInstance(session2.pending_pause, ApprovalRequest)

        final_reply = session2.resume(True)
        self.assertEqual(final_reply, "Resumed across process restart and updated value to 99.")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "value = 99\n")
        self.assertFalse(checkpoint_path.exists())
        # Verify both read-1 and edit-1 tool results are preserved in the resumed turn
        tool_call_ids = [m.get("call_id") for m in captured_after_resume[0] if m["role"] == "tool"]
        self.assertEqual(tool_call_ids, ["read-1", "edit-1"])

    def test_subagent_delegation_isolates_context_and_forbids_recursion(self) -> None:
        task_store = TaskStateStore(self.state_dir / "task.json")
        registry = self._build_full_registry(task_store)
        seen_requests = []
        replies = iter((
            # Parent calls delegate_subagent
            call_reply("sub-1", "delegate_subagent", role="explorer", task="Check value in app.py"),
            # Child explorer calls read
            call_reply("child-read-1", "read", path="app.py"),
            # Child explorer returns final summary
            ModelReply("app.py defines `value = 1` on line 1."),
            # Parent receives child summary and replies
            ModelReply("Explorer confirmed value is 1."),
        ))

        def complete(messages, offered, timeout):
            seen_requests.append((list(messages), tuple(t.name for t in offered)))
            return next(replies)

        session = ChatSession(complete, registry)
        runner = SubAgentRunner(
            complete_getter=session.get_model,
            registry=registry,
            approval_policy_getter=session.get_approval_policy,
            mode_getter=lambda: session.mode,
        )
        registry.register(DelegateSubAgentTool(runner))

        answer = session.send("Use explorer subagent to inspect app.py")
        self.assertEqual(answer, "Explorer confirmed value is 1.")
        # Request 1 (index 1) is the child subagent's first request:
        child_messages, child_tools = seen_requests[1]
        self.assertNotIn("delegate_subagent", child_tools)
        self.assertNotIn("write", child_tools)
        self.assertNotIn("edit", child_tools)
        self.assertIn("read", child_tools)
        # Parent's final request (index 3) only sees the subagent summary, NOT child-read-1 tool call
        parent_final_messages, _ = seen_requests[3]
        parent_tool_ids = [m.get("call_id") for m in parent_final_messages if m["role"] == "tool"]
        self.assertEqual(parent_tool_ids, ["sub-1"])

    def test_macos_sandbox_blocks_writes_outside_workspace_and_elevated_forces_approval(self) -> None:
        outside_dir = Path(self.temp.name) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "forbidden.txt"

        policy = SandboxPolicy(workspace=self.root)
        profile = build_seatbelt_profile(policy)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(str(self.root.resolve()), profile)

        # Elevated unsandboxed request must force ApprovalRequest even when approval_policy is "approve" (Auto mode)
        registry = ToolRegistry((BashTool(self.root),))
        replies = iter((
            call_reply("b1", "bash", command="echo hi", allow_unsandboxed=True),
            ModelReply("done"),
        ))
        session = ChatSession(lambda m, t, to: next(replies), registry)
        session.set_approval_policy(lambda: "approve")
        pause = session.send("run unsandboxed")
        self.assertIsInstance(pause, ApprovalRequest)
        self.assertTrue(pause.elevated)
        self.assertEqual(session.answer_approval(True), "done")

        if sandbox_available():
            # Verify macOS sandbox-exec actually blocks writing to outside_file
            tool = BashTool(self.root)
            action = tool.approval({"command": f"echo hacked > {outside_file}"}, lambda: None)
            with self.assertRaises(Exception):
                action.execute(lambda: None)
            self.assertFalse(outside_file.exists())

    def test_mcp_stdio_client_discovers_and_invokes_remote_tools(self) -> None:
        server_script = Path(self.temp.name) / "mock_mcp_server.py"
        server_script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    method = req.get('method')\n"
            "    rid = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {}}}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/list':\n"
            "        tools = [{'name': 'add_numbers', 'description': 'Add two integers.', 'readOnlyHint': True, 'inputSchema': {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b']}}]\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'tools': tools}}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/call':\n"
            "        args = req['params']['arguments']\n"
            "        total = args['a'] + args['b']\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': f'sum={total}'}]}})+ '\\n')\n"
            "        sys.stdout.flush()\n",
            encoding="utf-8",
        )
        manager = McpManager.from_config(
            {
                "math": {
                    "command": sys.executable,
                    "args": ["-u", str(server_script)],
                    "read_only": True,
                }
            },
            workspace=self.root,
        )
        self.addCleanup(manager.close)
        registry = ToolRegistry()
        registered = manager.register_tools(registry)
        self.assertEqual(registered, ["mcp__math__add_numbers"])

        replies = iter((
            call_reply("m1", "mcp__math__add_numbers", a=17, b=25),
            ModelReply("The sum is 42."),
        ))
        results = []
        session = ChatSession(
            lambda m, t, to: next(replies),
            registry,
            on_tool_result=lambda c, r: results.append(r.content),
        )
        self.assertEqual(session.send("Add 17 and 25"), "The sum is 42.")
        self.assertEqual(results, ["sum=42"])

    def test_memory_cross_category_correction_supersedes_user_preference(self) -> None:
        manager = MemoryManager(self.state_dir, "proj")
        tool = RememberMemoryTool(manager)
        tool.set_source_ref("sess-1", "turn-1", "以后记住我名叫蒋庆")
        res1 = tool.execute(
            {"content": "用户姓名是蒋庆", "category": "user_preference", "scope": "global"},
            lambda: None,
        )
        items = manager.list("global")
        self.assertEqual(len(items), 1)
        old_id = items[0].id

        # Now user corrects with natural phrasing "你说错了，我是蒋继庆" and model uses category="user_correction"
        tool.set_source_ref("sess-1", "turn-2", "你说错了，我实际上叫蒋继庆")
        res2 = tool.execute(
            {
                "content": "用户姓名是蒋继庆（此前误记为蒋庆）",
                "category": "user_correction",
                "scope": "global",
                "relation": "supersede",
                "related_ids": [old_id[:8]],
            },
            lambda: None,
        )
        self.assertIn("supersede", res2)
        active_items = manager.list("global")
        self.assertEqual(len(active_items), 1)
        self.assertIn("蒋继庆", active_items[0].content)
        all_items = manager.list("global", include_history=True)
        self.assertEqual(len(all_items), 2)

    def test_interactive_memory_menu_edits_and_deletes_by_number_without_knowing_id(self) -> None:
        manager = MemoryManager(self.state_dir, "proj")
        manager.remember(
            {"content": "偏好使用中文回答", "category": "user_preference", "scope": "global", "source_type": "user_instruction"},
            None,
        )
        manager.remember(
            {"content": "项目使用 unittest", "category": "project_fact", "scope": "project", "source_type": "agent_extraction"},
            None,
        )
        out = io.StringIO()
        # User enters `/memory`, sees numbered list (1=global, 2=project), selects '1', chooses 'e' (edit), types new text
        prompts = iter(["1", "e", "偏好使用简洁的中文回答"])
        with redirect_stdout(out):
            _manage_memory("/memory", manager, read_input=lambda _p: next(prompts))

        rendered = out.getvalue()
        self.assertIn("1. [全局/user_preference] 偏好使用中文回答", rendered)
        self.assertIn("2. [项目/project_fact] 项目使用 unittest", rendered)
        self.assertEqual(manager.list("global")[0].content, "偏好使用简洁的中文回答")

        # User deletes item #2 via interactive `/memory` -> `d 2`
        out2 = io.StringIO()
        with redirect_stdout(out2):
            _manage_memory("/memory", manager, read_input=lambda _p: "d 2")
        self.assertEqual(manager.list("project"), [])
        self.assertEqual(len(manager.list("global")), 1)

    def test_interactive_tools_menu_toggles_and_persists_tool_state(self) -> None:
        tools_state = self.state_dir / "tools.json"
        registry = create_builtin_registry(self.root)
        registry.bind_state_path(tools_state)

        out = io.StringIO()
        # Item 5 in builtin registry is `bash` (read=1, write=2, edit=3, tool_search=4, bash=5)
        with redirect_stdout(out):
            _manage_tools("/tools", registry, read_input=lambda _p: "5")
        self.assertIn("已停用工具：bash", out.getvalue())
        self.assertFalse(registry.is_enabled("bash"))
        self.assertNotIn("bash", [t.name for t in registry.all()])

        # Verify persistence across a new ToolRegistry instance
        registry2 = create_builtin_registry(self.root)
        registry2.bind_state_path(tools_state)
        self.assertFalse(registry2.is_enabled("bash"))

        # Re-enable by number
        with redirect_stdout(io.StringIO()):
            _manage_tools("/tools", registry2, read_input=lambda _p: "5")
        self.assertTrue(registry2.is_enabled("bash"))

    def test_interactive_skill_and_mcp_menus_toggle_by_number_and_persist(self) -> None:
        # 1. Skill interactive menu by number
        skill_file = self.state_dir / "skills/code-audit/SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "---\nname: code-audit\ndescription: Audit code security.\n---\nCheck inputs.\n",
            encoding="utf-8",
        )
        skills = SkillRegistry(
            self.root,
            skills_root=self.state_dir / "skills",
            state_path=self.state_dir / "skills.json",
        )
        out_skill = io.StringIO()
        prompts = iter(["1", "t"])
        with redirect_stdout(out_skill):
            _manage_skills("/skill", skills, read_input=lambda _p: next(prompts))
        self.assertFalse(skills.all_skills()[0].enabled)
        with redirect_stdout(io.StringIO()):
            _manage_skills("/skill", skills, read_input=lambda _p: "t 1")
        self.assertTrue(skills.all_skills()[0].enabled)

        # 2. MCP interactive menu by number
        server_script = Path(self.temp.name) / "mock_mcp_toggle.py"
        server_script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    rid = req.get('id')\n"
            "    if req.get('method') == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {}}}) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif req.get('method') == 'tools/list':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': {'tools': [{'name': 'ping', 'description': 'Ping server'}]}})+ '\\n')\n"
            "        sys.stdout.flush()\n",
            encoding="utf-8",
        )
        mcp_state = self.state_dir / "mcp.json"
        manager = McpManager.from_config(
            {"demo": {"command": sys.executable, "args": ["-u", str(server_script)], "read_only": True}},
            workspace=self.root,
            state_path=mcp_state,
        )
        self.addCleanup(manager.close)
        reg = ToolRegistry()
        manager.register_tools(reg)
        self.assertIsNotNone(reg.get("mcp__demo__ping"))

        out_mcp = io.StringIO()
        with redirect_stdout(out_mcp):
            _manage_mcp("/mcp", manager, reg, read_input=lambda _p: "1")
        self.assertIn("已停用 MCP Server：demo", out_mcp.getvalue())
        self.assertFalse(manager.is_server_enabled("demo"))
        self.assertIsNone(reg.get("mcp__demo__ping"))

        # Re-enable by number
        with redirect_stdout(io.StringIO()):
            _manage_mcp("/mcp", manager, reg, read_input=lambda _p: "1")
        self.assertTrue(manager.is_server_enabled("demo"))
        self.assertIsNotNone(reg.get("mcp__demo__ping"))
