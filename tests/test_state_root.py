import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from src.harness.memory.store import migrate_legacy_state, state_root


class StateRootTests(TestCase):
    def test_default_state_directory_is_minicc_home_even_when_xdg_is_set(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            xdg = Path(temporary) / "xdg-state"
            home.mkdir()
            with patch.dict(os.environ, {"HOME": str(home), "XDG_STATE_HOME": str(xdg)}):
                self.assertEqual(state_root(), home / ".minicc")

    def test_existing_state_is_migrated_from_legacy_xdg_directory(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            xdg = Path(temporary) / "xdg-state"
            legacy = xdg / "minicc"
            legacy.mkdir(parents=True)
            (legacy / "memories.json").write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {"HOME": str(home), "XDG_STATE_HOME": str(xdg)}):
                target = state_root()
                self.assertTrue(legacy.exists(), "path lookup must not move user data")
                migrate_legacy_state()

            self.assertEqual(target, home / ".minicc")
            self.assertEqual((target / "memories.json").read_text(encoding="utf-8"), "[]")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertFalse(legacy.exists())

    def test_migration_preserves_existing_destination_files(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            xdg = Path(temporary) / "xdg-state"
            target = home / ".minicc"
            legacy = xdg / "minicc"
            target.mkdir(parents=True)
            legacy.mkdir(parents=True)
            (target / "settings.json").write_text("new", encoding="utf-8")
            (legacy / "memories.json").write_text("[]", encoding="utf-8")
            (legacy / "settings.json").write_text("old", encoding="utf-8")
            with patch.dict(os.environ, {"HOME": str(home), "XDG_STATE_HOME": str(xdg)}):
                state_root()
                migrate_legacy_state()

            self.assertEqual((target / "memories.json").read_text(encoding="utf-8"), "[]")
            self.assertEqual((target / "settings.json").read_text(encoding="utf-8"), "new")
            self.assertEqual((target / ".legacy-conflicts/settings.json").read_text(encoding="utf-8"), "old")
            self.assertEqual((legacy / "settings.json").read_text(encoding="utf-8"), "old")

    def test_completed_migration_keeps_the_state_directory_private(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            xdg = Path(temporary) / "xdg-state"
            target = home / ".minicc"
            legacy = xdg / "minicc"
            target.mkdir(parents=True)
            legacy.mkdir(parents=True)
            (target / ".legacy-state-migrated").write_text("done", encoding="utf-8")
            target.chmod(0o755)
            with patch.dict(os.environ, {"HOME": str(home), "XDG_STATE_HOME": str(xdg)}):
                migrate_legacy_state()

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
