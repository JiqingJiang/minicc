import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.harness.memory.store import MemoryStore


class MemoryStoreReviewFixTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root / "memories.json", "project")
        patcher = patch("src.harness.memory.store.tokenize", side_effect=lambda text: text.casefold().split())
        patcher.start()
        self.addCleanup(patcher.stop)

    def remember(self, content, **kwargs):
        return self.store.remember(content, category="project_fact", source_type="agent_extraction", source_ref=None, **kwargs)

    def test_agent_cannot_supersede_by_claiming_user_source(self):
        old, _ = self.remember("Use the stable endpoint")
        with self.assertRaisesRegex(ValueError, "可信来源"):
            self.store.remember(
                "Use the new endpoint", category="project_fact", source_type="user_correction",
                source_ref=None, relation="supersede", related_ids=[old.id],
            )
        self.assertEqual(self.store.get(old.id).status, "active")

    def test_trusted_explicit_user_source_can_supersede(self):
        old, _ = self.remember("Use the stable endpoint")
        source = json.dumps({"session_id": "s", "turn_id": "t", "trusted_source_type": "user_instruction"})
        new, _ = self.store.remember(
            "Use the new endpoint", category="project_fact", source_type="user_instruction",
            source_ref=source, relation="supersede", related_ids=[old.id],
        )
        self.assertEqual(self.store.get(old.id, include_deleted=True).status, "superseded")
        self.assertEqual(self.store.get(new.id).content, "Use the new endpoint")

    def test_concurrent_writes_from_separate_store_instances_are_preserved(self):
        def write(index):
            store = MemoryStore(self.root / "memories.json", "project")
            store.remember(
                f"Fact number {index}", category="project_fact", source_type="agent_extraction",
                source_ref=None,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(24)))
        contents = {item.content for item in self.store.list()}
        self.assertEqual(contents, {f"Fact number {index}" for index in range(24)})
        self.assertFalse(list(self.root.glob("*.tmp")))
