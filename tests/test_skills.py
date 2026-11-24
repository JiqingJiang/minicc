import base64
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.parse import parse_qs, urlparse

from src.client.terminal_chat import _manage_skills
import src.harness.skills as skill_api
from src.harness.memory.manager import MemoryManager
from src.harness.memory.remember_tool import RememberMemoryTool, SearchMemoryTool
from src.harness.skills import (
    InstallSkillTool,
    LoadSkillTool,
    SearchSkillsTool,
    SkillRegistry,
    SkillsCatalogClient,
)
from src.harness.tools.base import ToolCall
from src.harness.tools.builtins import create_builtin_registry
from src.harness.tools.executor import PendingApproval, ToolExecutor


class SkillRegistryTests(TestCase):
    def test_default_skills_root_does_not_raise_name_error(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = SkillRegistry(root, state_path=root / "skills.json")
            self.assertTrue(str(registry.skills_root).endswith("skills"))

    def test_load_skill_is_offered_with_the_default_harness_tool_set(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = create_builtin_registry(root)
            memory = MemoryManager(root / "state", "project", None)
            skills = SkillRegistry(root, skills_root=root / "skills", state_path=root / "skills.json")
            catalog = SkillsCatalogClient(request_json=lambda _url: {"data": []})
            registry.register(RememberMemoryTool(memory))
            registry.register(SearchMemoryTool(memory))
            registry.register(SearchSkillsTool(catalog))
            registry.register(InstallSkillTool(catalog, skills))
            registry.register(LoadSkillTool(skills))

            offered = ToolExecutor().offered_tools(registry.all())

            self.assertTrue({"search_skills", "install_skill", "load_skill"}.issubset(
                {tool.name for tool in offered}
            ))

    def test_install_skill_requires_approval_in_executor(self):
        class Catalog:
            def fetch(self, skill_id):
                return {
                    "SKILL.md": "---\nname: approved-skill\ndescription: Safe skill.\n---\nBody.\n",
                }

        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = SkillRegistry(root, skills_root=root / "skills", state_path=root / "state.json")
            tool = InstallSkillTool(Catalog(), registry)
            executor = ToolExecutor()
            step = executor.execute(
                ToolCall("inst-1", "install_skill", '{"skill_id":"owner/approved-skill"}'),
                (tool,),
                lambda: None,
            )
            self.assertIsInstance(step, PendingApproval)
            self.assertEqual(registry.all_skills(), [])
            res = executor.resume(step, True, lambda: None)
            self.assertIsNone(res.error_code)
            self.assertEqual(len(registry.all_skills()), 1)

    def test_online_catalog_search_returns_installable_skill_ids(self):
        client_type = getattr(skill_api, "SkillsCatalogClient", None)
        self.assertIsNotNone(client_type)
        requested_urls = []

        def request_json(url):
            requested_urls.append(url)
            return {"q": "review Python test quality", "items": [{
                "slug": "affaan-m/python-testing",
                "type": "single",
                "title": "python-testing",
                "description": "Python testing strategies and coverage.",
                "verified": True,
                "agents": "claude-code,codex",
                "category": "Coding & Dev Tools",
                "avg_rating": 4.8,
                "rating_count": 12,
                "raw_url": "https://api.skillmd.com/api/skills/affaan-m/python-testing/raw",
            }]}

        client = client_type(request_json=request_json)
        result = SearchSkillsTool(client).execute(
            {"query": "review Python test quality", "limit": 3}, lambda: None,
        )
        results = json.loads(result)

        self.assertEqual(results[0]["id"], "affaan-m/python-testing")
        self.assertEqual(results[0]["name"], "python-testing")
        self.assertEqual(results[0]["description"], "Python testing strategies and coverage.")
        self.assertTrue(results[0]["verified_publisher"])
        self.assertIn("/v1/search?", requested_urls[0])
        self.assertEqual(parse_qs(urlparse(requested_urls[0]).query),
                         {"q": ["review Python test quality"], "limit": ["3"]})

    def test_catalog_fetch_returns_all_skill_package_files(self):
        skill_id = "affaan-m/python-testing"
        skill_markdown = "---\nname: python-testing\ndescription: Review Python.\n---\nCheck tests.\n"
        checklist = "Inspect edge cases.\n"
        requested_urls = []

        def request_json(url):
            requested_urls.append(url)
            if "/v1/skills/" in url:
                return {
                    "slug": skill_id,
                    "type": "single",
                    "verified": True,
                    "license": "MIT",
                    "source_repo": "https://github.com/affaan-m/python-testing",
                    "commit_sha": "abc123",
                }
            return {
                "slug": skill_id,
                "files": [
                    {"path": "SKILL.md", "content_base64": base64.b64encode(skill_markdown.encode()).decode(),
                     "sha256": hashlib.sha256(skill_markdown.encode()).hexdigest(), "storage": "inline"},
                    {"path": "references/checklist.md", "content_base64": base64.b64encode(checklist.encode()).decode(),
                     "sha256": hashlib.sha256(checklist.encode()).hexdigest(), "storage": "inline"},
                ],
            }

        files = SkillsCatalogClient(request_json=request_json).fetch(skill_id)

        self.assertEqual(files["references/checklist.md"], checklist)
        self.assertTrue(requested_urls[0].endswith("/v1/skills/affaan-m/python-testing"))
        self.assertTrue(requested_urls[1].endswith("/api/skills/affaan-m/python-testing/bundle?format=json"))

    def test_catalog_retries_one_transient_search_timeout(self):
        attempts = []

        def request_json(_url):
            attempts.append(True)
            if len(attempts) == 1:
                raise TimeoutError("temporary catalog timeout")
            return {"items": [{
                "slug": "github/pytest-coverage", "type": "single", "title": "pytest-coverage",
                "description": "Inspect test coverage.", "verified": True,
            }]}

        results = SkillsCatalogClient(request_json=request_json).search("python test coverage")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(results[0].id, "github/pytest-coverage")

    def test_catalog_rejects_skill_files_with_a_mismatched_hash(self):
        skill_id = "github/pytest-coverage"
        manifest = "---\nname: pytest-coverage\ndescription: Inspect coverage.\n---\nRun coverage.\n"
        encoded = base64.b64encode(manifest.encode()).decode()
        responses = iter((
            {"slug": skill_id, "type": "single"},
            {"slug": skill_id, "files": [{"path": "SKILL.md", "content_base64": encoded,
                                            "sha256": "0" * 64, "storage": "inline"}]},
        ))

        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            SkillsCatalogClient(request_json=lambda _url: next(responses)).fetch(skill_id)

    def test_installing_catalog_skill_registers_it_in_minicc_library(self):
        class Catalog:
            def fetch(self, skill_id):
                self.skill_id = skill_id
                return {
                    "SKILL.md": "---\nname: python-review\ndescription: Review Python tests.\ntags: [python, testing]\n---\nCheck missing tests.\n",
                    "references/checklist.md": "Check assertions and edge cases.\n",
                }

        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = SkillRegistry(root, skills_root=root / "skills", state_path=root / "state.json")
            tool = InstallSkillTool(Catalog(), registry)

            result = tool.execute({"skill_id": "affaan-m/python-testing"}, lambda: None)

            installed = registry.load("python-review")
            self.assertIn("python-review", result)
            self.assertIsNotNone(installed)
            self.assertIn("Check missing tests.", installed.content)
            self.assertEqual((root / "skills/python-review/references/checklist.md").read_text(),
                             "Check assertions and edge cases.\n")

    def test_installer_rejects_remote_package_paths_that_escape_skill_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            registry = SkillRegistry(root, skills_root=root / "skills", state_path=root / "state.json")
            files = {
                "SKILL.md": "---\nname: unsafe-skill\ndescription: Unsafe package.\n---\nDo work.\n",
                "../escaped.txt": "outside",
            }

            with self.assertRaises(ValueError):
                registry.install_package("source/unsafe-skill", files)

            self.assertFalse((root / "escaped.txt").exists())

    def test_registry_ignores_skill_files_in_other_agent_directories(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            (workspace / ".git").mkdir(parents=True)
            self._write_skill(workspace / ".claude/skills/web-research/SKILL.md", "web-research",
                              "Research current information on the web.", "Claude instructions.", ["web", "research"])
            self._write_skill(root / "skills/python-review/SKILL.md", "python-review",
                              "Review Python project tests.", "Review test coverage.", ["python", "testing"])

            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json")

            skills = registry.all_skills()
            self.assertEqual([skill.name for skill in skills], ["python-review"])
            self.assertEqual(skills[0].source, "minicc")

    def test_retrieval_ranks_name_and_capability_tags_and_returns_top_k(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            self._write_skill(root / "skills/pdf-ocr/SKILL.md", "pdf-ocr",
                              "Extract text from images.", "OCR workflow.", ["ocr", "文档识别"])
            self._write_skill(root / "skills/web-research/SKILL.md", "web-research",
                              "Research the current web.", "Web workflow.", ["web", "research"])
            self._write_skill(root / "skills/release-check/SKILL.md", "release-check",
                              "Verify build and release artifacts.", "Release workflow.", ["release", "verification"])
            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json")

            pdf = registry.match("请从扫描图片里做 OCR 文档识别", top_k=1)
            web = registry.match("research current webpage", top_k=1)

            self.assertEqual(pdf[0].name, "pdf-ocr")
            self.assertEqual(web[0].name, "web-research")
            self.assertGreater(pdf[0].score, 0)

    def test_semantic_embedding_recalls_skill_without_shared_keywords(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            self._write_skill(root / "skills/architecture-map/SKILL.md", "architecture-map",
                              "Create diagrams of software architecture.", "Map components and their dependencies.", ["diagram"])

            def embed(text):
                vector = [0.0, 1.0] if "picture" in text or "diagrams" in text or "diagram" in text else [1.0, 0.0]
                return "test-model", vector

            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json", embedder=embed)
            result = registry.match("draw me a picture", top_k=1)

            self.assertEqual(result[0].name, "architecture-map")
            self.assertIn("semantic similarity", result[0].reasons)

    def test_respects_model_and_user_invocation_flags(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            self._write_skill(root / "skills/manual-release/SKILL.md", "manual-release",
                              "Prepare a release.", "Manual release workflow.", ["release"])
            path = root / "skills/manual-release/SKILL.md"
            path.write_text(path.read_text(encoding="utf-8").replace(
                "tags: [release]", "tags: [release]\ndisable-model-invocation: true"), encoding="utf-8")
            self._write_skill(root / "skills/background-context/SKILL.md", "background-context",
                              "Explain project architecture.", "Background reference.", ["architecture"])
            background = root / "skills/background-context/SKILL.md"
            background.write_text(background.read_text(encoding="utf-8").replace(
                "tags: [architecture]", "tags: [architecture]\nuser-invocable: false"), encoding="utf-8")
            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json")

            self.assertIsNone(registry.load("manual-release"))
            self.assertIsNotNone(registry.load("manual-release", user_initiated=True))
            self.assertIsNotNone(registry.load("background-context"))
            self.assertIsNone(registry.load("background-context", user_initiated=True))

    def test_parses_tags_and_folded_multiline_from_agent_metadata_map(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            manifest = root / "skills/office-workflow/SKILL.md"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                "---\nname: office-workflow\ndescription: >\n  Format business\n  reports cleanly.\n"
                "metadata:\n  tags:\n    - document\n    - office\n---\nUse a report template.\n",
                encoding="utf-8",
            )

            skill = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json").all_skills()[0]

            self.assertEqual(skill.tags, ("document", "office"))
            self.assertEqual(skill.description, "Format business reports cleanly.")

    def test_enable_disable_and_global_switch_are_persistent(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            self._write_skill(root / "skills/web-research/SKILL.md", "web-research",
                              "Research current web sources.", "Search and summarize.", ["web"])
            state_path = root / "state.json"
            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=state_path)

            self.assertTrue(registry.disable("web-research"))
            self.assertEqual(registry.match("research web sources"), [])
            self.assertTrue(SkillRegistry(workspace, skills_root=root / "skills", state_path=state_path).enable("web-research"))
            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=state_path)
            registry.set_enabled(False)
            self.assertEqual(registry.match("research web sources"), [])
            self.assertFalse(SkillRegistry(workspace, skills_root=root / "skills", state_path=state_path).enabled)
            self.assertTrue(SkillRegistry(workspace, skills_root=root / "skills", state_path=state_path).set_enabled(True))

    def test_terminal_commands_toggle_skills_and_explicitly_run_user_skill(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            self._write_skill(root / "skills/release_check/SKILL.md", "release_check",
                              "Validate a release.", "Release steps.", ["release"])
            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json")

            with redirect_stdout(StringIO()):
                _manage_skills("/skill disable release_check", registry)
                self.assertFalse(registry.all_skills()[0].enabled)
                _manage_skills("/skill enable release_check", registry)
                explicit = _manage_skills("/skill use release_check publish version 2", registry)
                _manage_skills("/skill off", registry)

            self.assertIn("Release steps", explicit)
            self.assertIn("publish version 2", explicit)
            self.assertFalse(registry.enabled)

    def test_loads_full_skill_and_relative_resource_only_after_request(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            skill_dir = root / "skills/release-check"
            self._write_skill(skill_dir / "SKILL.md", "release-check", "Validate a release.",
                              "Read the deployment checklist.", ["release"])
            (skill_dir / "references").mkdir()
            (skill_dir / "references/checklist.md").write_text("Checklist content.", encoding="utf-8")
            registry = SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json")

            loaded = registry.load("release-check")
            resource = registry.load("release-check", resource_path="references/checklist.md")

            self.assertIn("Read the deployment checklist", loaded.content)
            self.assertEqual(resource.content, "Checklist content.")
            with self.assertRaises(ValueError):
                registry.load("release-check", resource_path="../../outside.txt")

    def test_load_tool_returns_skill_instructions_and_can_read_references(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "project"
            skill_dir = root / "skills/release-check"
            self._write_skill(skill_dir / "SKILL.md", "release-check", "Validate releases.",
                              "Run the release checklist.", ["release"])
            (skill_dir / "references").mkdir()
            (skill_dir / "references/checklist.md").write_text("Reference checklist.", encoding="utf-8")
            tool = LoadSkillTool(SkillRegistry(workspace, skills_root=root / "skills", state_path=root / "state.json"))

            instructions = tool.execute({"name": "release-check"}, lambda: None)
            reference = tool.execute({"name": "release-check", "file_path": "references/checklist.md"}, lambda: None)

            self.assertIn("Run the release checklist", instructions)
            self.assertIn("Reference checklist", reference)

    @staticmethod
    def _write_skill(path: Path, name: str, description: str, body: str, tags: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\ntags: [{', '.join(tags)}]\n---\n{body}\n",
            encoding="utf-8",
        )
