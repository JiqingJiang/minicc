"""Persistent user controls for Skills."""

from __future__ import annotations

import json
from pathlib import Path

from src.adapters.storage.json_file import atomic_write_json, file_lock
from src.harness.memory.store import state_root


class SkillStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_root() / "skills.json"

    def enabled(self) -> bool:
        return bool(self._read().get("enabled", True))

    def set_enabled(self, enabled: bool) -> None:
        self._update(lambda data: data.update(enabled=bool(enabled)))

    def disabled(self) -> set[str]:
        values = self._read().get("disabled", [])
        return {item for item in values if isinstance(item, str)} if isinstance(values, list) else set()

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> None:
        def update(data: dict) -> None:
            disabled = {item for item in data.get("disabled", []) if isinstance(item, str)}
            if enabled:
                disabled.discard(skill_id)
            else:
                disabled.add(skill_id)
            data["disabled"] = sorted(disabled)

        self._update(update)

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _update(self, updater) -> None:
        with file_lock(self.path):
            data = self._read()
            updater(data)
            atomic_write_json(self.path, data)
