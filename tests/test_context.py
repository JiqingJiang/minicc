from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.harness.context.assembler import ContextAssembler, ContextBudget
from src.harness.context.prompt import AgentProfile, PromptCatalog
from src.harness.context.skills import Skill, SkillProvider
from src.harness.memory.manager import MemoryManager
from src.harness.tools.base import ToolCall


class StaticSkills(SkillProvider):
    def select(self, query: str, *, agent_id: str) -> tuple[Skill, ...]:
        return (Skill("writing", "Writing conventions", "Use concise Chinese.", "test-skill"),)


class ContextAssemblerTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.memory = MemoryManager(self.state, "project")

    def assembler(self, **kwargs):
        return ContextAssembler(
            self.root,
            trace_path=self.state / "traces.jsonl",
            memory=self.memory,
            **kwargs,
        )

    def test_loads_global_hidden_and_workspace_instruction_files(self):
        home = Path(self.temp.name) / "home"
        (home / ".minicc").mkdir(parents=True)
        (home / ".minicc" / "MINICC.md").write_text("Global rules", encoding="utf-8")
        (self.root / "MINICC.md").write_text("Project rules", encoding="utf-8")
        with patch("src.harness.context.assembler.Path.home", return_value=home):
            result = self.assembler().assemble([{"role": "user", "content": "hello"}])
        context = "\n".join(str(message.get("content", "")) for message in result.messages)
        self.assertIn("Global rules", context)
        self.assertIn("Project rules", context)

    def test_uses_profile_specific_system_prompt(self):
        catalog = PromptCatalog((
            AgentProfile("default", "MiniCC", "General assistant", "Base prompt."),
            AgentProfile("research", "Researcher", "Find evidence", "Research-only prompt."),
        ))
        result = self.assembler(prompt_catalog=catalog, agent_id="research").assemble(
            [{"role": "user", "content": "Find sources"}]
        )
        system = "\n".join(str(message.get("content", "")) for message in result.messages if message["role"] == "system")
        self.assertIn("Research-only prompt.", system)
        self.assertNotIn("Base prompt.", system)

    def test_prompt_catalog_reads_agent_profiles_and_rejects_unknown_agents(self):
        catalog = PromptCatalog.from_config({
            "reviewer": {"name": "Reviewer", "purpose": "Review changes", "system_prompt": "Review prompt."}
        })
        self.assertEqual(catalog.get("reviewer").system_prompt, "Review prompt.")
        with self.assertRaisesRegex(ValueError, "not registered"):
            catalog.get("missing")

    def test_skill_provider_injects_selected_skill_and_records_its_source(self):
        catalog = PromptCatalog((
            AgentProfile("default", "MiniCC", "General assistant", "Base prompt."),
            AgentProfile("writer", "Writer", "Write documents", "Writing prompt."),
        ))
        result = self.assembler(skill_provider=StaticSkills(), prompt_catalog=catalog, agent_id="writer",
                                budget=ContextBudget(max_skill_chars=10)).assemble(
            [{"role": "user", "content": "Write a report"}]
        )
        content = "\n".join(str(message.get("content", "")) for message in result.messages)
        self.assertIn("Use concis", content)
        skill_source = next(source for source in result.trace.sources if source.get("kind") == "skill")
        self.assertTrue(skill_source["items"][0]["truncated"])

    def test_context_budget_loads_explicit_settings_and_rejects_unknown_fields(self):
        budget = ContextBudget.from_config({"context_window_tokens": 64000, "output_tokens": 2000})
        self.assertEqual(budget.available_input_tokens, 60976)
        with self.assertRaisesRegex(ValueError, "未知字段"):
            ContextBudget.from_config({"typo": 1})

    def test_compression_drops_complete_old_turns_and_keeps_recent_tool_pair(self):
        budget = ContextBudget(context_window_tokens=900, output_tokens=100, reserve_tokens=100,
                               compression_threshold=0.65, compression_target=0.45,
                               min_recent_turns=1, summary_max_chars=500)
        messages = [
            {"role": "user", "content": "old " + "x" * 900, "_source_turn_id": "old"},
            {"role": "assistant", "content": "old answer", "_source_turn_id": "old"},
            {"role": "user", "content": "current request", "_source_turn_id": "current"},
            {"role": "assistant", "content": None, "tool_calls": (ToolCall("call-1", "read", "{}"),)},
            {"role": "tool", "content": "result " + "y" * 400, "call_id": "call-1"},
            {"role": "assistant", "content": "done"},
        ]
        result = self.assembler(budget=budget).assemble(messages)
        output = result.messages
        self.assertTrue(result.trace.summarized)
        self.assertFalse(any(message.get("_source_turn_id") == "old" for message in output))
        self.assertEqual([message.get("call_id") for message in output if message["role"] == "tool"], ["call-1"])
        self.assertEqual([call.call_id for message in output for call in message.get("tool_calls", ())], ["call-1"])
        self.assertTrue(any(source.get("kind") == "summary" for source in result.trace.sources))
        self.assertLessEqual(result.trace.estimated_tokens, budget.available_input_tokens)

    def test_compression_trims_oversized_tool_output_without_losing_call_id(self):
        budget = ContextBudget(context_window_tokens=1500, output_tokens=200, reserve_tokens=100,
                               compression_threshold=0.5, compression_target=0.4,
                               tool_result_max_chars=256)
        messages = [
            {"role": "user", "content": "Inspect this", "_source_turn_id": "current"},
            {"role": "assistant", "content": None, "tool_calls": (ToolCall("call-1", "read", "{}"),)},
            {"role": "tool", "content": "evidence " * 200, "call_id": "call-1"},
            {"role": "assistant", "content": "Done"},
        ]
        result = self.assembler(budget=budget).assemble(messages)
        tool_result = next(message for message in result.messages if message["role"] == "tool")
        self.assertEqual(tool_result["call_id"], "call-1")
        self.assertLess(len(tool_result["content"]), 400)
        self.assertTrue(any(source.get("kind") == "tool_result_trim" for source in result.trace.sources))
