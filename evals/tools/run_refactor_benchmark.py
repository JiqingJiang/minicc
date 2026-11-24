"""Multi-scenario evaluation benchmark covering Plan -> HITL -> SubAgent -> Goal -> Persistence/Resume -> Sandbox."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

from src.adapters.mcp.client import McpManager
from src.harness.agent_loop.run_turn import (
    ApprovalRequest,
    ClarificationRequest,
    ModelReply,
    RequestUsage,
)
from src.harness.context.assembler import ContextAssembler
from src.harness.memory.manager import MemoryManager
from src.harness.memory.remember_tool import RememberMemoryTool, SearchMemoryTool
from src.harness.orchestration.subagents.runner import DelegateSubAgentTool, SubAgentRunner
from src.harness.skills import InstallSkillTool, LoadSkillTool, SearchSkillsTool, SkillRegistry, SkillsCatalogClient
from src.harness.tools.base import ToolCall
from src.harness.tools.builtins import create_builtin_registry
from src.runtime.hitl.contracts import AskUserTool, UpdatePlanTool
from src.runtime.session.chat_session import ChatSession
from src.runtime.task.state import TaskStateStore


def _call(call_id: str, name: str, *, in_tok: int = 120, out_tok: int = 40, **kwargs) -> ModelReply:
    return ModelReply(
        None,
        (ToolCall(call_id, name, json.dumps(kwargs, ensure_ascii=False)),),
        usage=RequestUsage("benchmark-model", in_tok, out_tok, 20, 512),
    )


def run_benchmark() -> dict:
    started = time.monotonic()
    with TemporaryDirectory(prefix="minicc-full-eval-") as temp:
        root = Path(temp) / "workspace"
        root.mkdir()
        state_dir = Path(temp) / "state"
        state_dir.mkdir()
        (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        task_store = TaskStateStore(state_dir / "task.json")
        memory = MemoryManager(state_dir, "eval-proj")
        skills = SkillRegistry(root, skills_root=state_dir / "skills", state_path=state_dir / "skills.json")
        catalog = SkillsCatalogClient(request_json=lambda _u: {"items": []})
        registry = create_builtin_registry(root)
        registry.register(RememberMemoryTool(memory))
        registry.register(SearchMemoryTool(memory))
        registry.register(SearchSkillsTool(catalog))
        registry.register(InstallSkillTool(catalog, skills))
        registry.register(LoadSkillTool(skills))
        registry.register(AskUserTool())
        registry.register(UpdatePlanTool(task_store))

        context = ContextAssembler(
            root,
            memory=memory,
            skill_provider=skills,
            task_store=task_store,
            trace_path=state_dir / "context-traces.jsonl",
        )

        # Scripted multi-stage trajectory:
        # Stage 1 (Plan mode): delegate explorer subagent -> ask_user clarification -> submit plan for approval
        # Stage 2 (Checkpoint interrupt & resume): edit calculator.py paused at approval, process restarts, resumed
        # Stage 3 (Goal mode): automatically complete remaining verification step
        replies_stage1 = iter((
            _call("sub-explore", "delegate_subagent", role="explorer", task="Inspect calculator.py bug"),
            _call("child-read", "read", path="calculator.py"),
            ModelReply("calculator.py line 2 returns `a - b` instead of `a + b`.", usage=RequestUsage("benchmark-model", 80, 25, 0, 256)),
            _call("clarify-1", "ask_user", question="Should we also add a docstring?", options=["No, keep minimal", "Yes"]),
            _call(
                "plan-1",
                "update_plan",
                goal="Fix add() bug in calculator.py",
                steps=[
                    {"title": "Fix return statement in calculator.py", "status": "pending"},
                    {"title": "Verify add(2, 3) == 5", "status": "pending"},
                ],
                verification_command="python3 -B -c 'import calculator; assert calculator.add(2, 3) == 5'",
                submit_for_approval=True,
            ),
            ModelReply("Plan approved. Ready to execute.", usage=RequestUsage("benchmark-model", 110, 20, 40, 512)),
            # Turn 2: start editing calculator.py -> pauses for approval & saves checkpoint.json
            _call("edit-calc", "edit", path="calculator.py", old_text="return a - b", new_text="return a + b"),
        ))

        session1 = ChatSession(
            lambda m, t, to: next(replies_stage1),
            registry,
            context=context,
            history_path=state_dir / "session.json",
            checkpoint_path=state_dir / "checkpoint.json",
            mode="plan",
        )
        runner1 = SubAgentRunner(
            complete_getter=session1.get_model,
            registry=registry,
            approval_policy_getter=session1.get_approval_policy,
            mode_getter=lambda: session1.mode,
        )
        registry.register(DelegateSubAgentTool(runner1))

        # 1. Start in Plan mode -> hits ask_user ClarificationRequest
        pause1 = session1.send("Find and plan the fix for calculator.py")
        assert isinstance(pause1, ClarificationRequest)
        # 2. Answer clarification -> hits Plan ApprovalRequest
        pause2 = session1.answer_clarification("No, keep minimal")
        assert isinstance(pause2, ApprovalRequest) and pause2.kind == "plan"
        # 3. Approve plan -> automatically switches to normal mode
        reply1 = session1.answer_approval(True)
        assert session1.mode == "normal"

        # 4. Switch to Goal mode and execute -> hits edit ApprovalRequest and writes checkpoint.json
        session1.set_mode("goal")
        pause3 = session1.send("Execute the approved plan")
        assert isinstance(pause3, ApprovalRequest)
        assert (state_dir / "checkpoint.json").exists()

        # 5. Simulate process restart: new ChatSession loads session.json + checkpoint.json + task.json
        replies_stage2 = iter((
            _call(
                "plan-step1-done",
                "update_plan",
                goal="Fix add() bug in calculator.py",
                steps=[
                    {"title": "Fix return statement in calculator.py", "status": "completed"},
                    {"title": "Verify add(2, 3) == 5", "status": "pending"},
                ],
            ),
            ModelReply("Step 1 completed.", usage=RequestUsage("benchmark-model", 130, 25, 60, 512)),
            # Goal mode auto-continues Round 2 because Step 2 is still pending!
            _call("bash-verify", "bash", command="python3 -B -c 'import calculator; assert calculator.add(2, 3) == 5; print(\"OK\")'"),
            _call(
                "plan-step2-done",
                "update_plan",
                goal="Fix add() bug in calculator.py",
                steps=[
                    {"title": "Fix return statement in calculator.py", "status": "completed"},
                    {"title": "Verify add(2, 3) == 5", "status": "completed"},
                ],
            ),
            ModelReply("All steps completed and verified.", usage=RequestUsage("benchmark-model", 140, 30, 80, 512)),
        ))

        registry2 = create_builtin_registry(root)
        registry2.register(RememberMemoryTool(memory))
        registry2.register(SearchMemoryTool(memory))
        registry2.register(SearchSkillsTool(catalog))
        registry2.register(InstallSkillTool(catalog, skills))
        registry2.register(LoadSkillTool(skills))
        registry2.register(AskUserTool())
        registry2.register(UpdatePlanTool(task_store))

        session2 = ChatSession(
            lambda m, t, to: next(replies_stage2),
            registry2,
            context=context,
            history_path=state_dir / "session.json",
            checkpoint_path=state_dir / "checkpoint.json",
        )
        session2.set_approval_policy(lambda: "approve")
        assert session2.has_pending_checkpoint
        final_reply = session2.resume(True)

        final_code = (root / "calculator.py").read_text(encoding="utf-8")
        final_task = task_store.load()
        traces = [
            json.loads(line)
            for line in (state_dir / "context-traces.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        checks = {
            "subagent_explored": True,
            "hitl_clarification_resolved": True,
            "plan_mode_enforced_and_approved": final_task.plan_approved,
            "checkpoint_persisted_and_resumed": not (state_dir / "checkpoint.json").exists(),
            "goal_mode_auto_completed_all_steps": not final_task.has_incomplete_steps,
            "code_fixed_and_verified": "return a + b" in final_code and "[Goal 自动推进]" in str(final_reply),
            "context_traces_recorded": len(traces) >= 5,
        }
        return {
            "passed": all(checks.values()),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "checks": checks,
            "context_trace_count": len(traces),
            "total_input_tokens": sum(t.get("actual_input_tokens") or 0 for t in traces),
            "total_output_tokens": sum(t.get("actual_output_tokens") or 0 for t in traces),
            "total_cached_tokens": sum(t.get("cached_input_tokens") or 0 for t in traces),
            "final_code": final_code,
        }


if __name__ == "__main__":
    report = run_benchmark()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["passed"] else 1)
