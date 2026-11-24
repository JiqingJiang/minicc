"""Shared data contracts for Skill discovery and loading."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    content: str | None
    source: str
    description: str = ""
    tags: tuple[str, ...] = ()
    path: Path | None = None
    enabled: bool = True
    model_invocable: bool = True
    user_invocable: bool = True
    score: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def body(self) -> str | None:
        return self.content


class SkillProvider(Protocol):
    def select(self, query: str, *, agent_id: str, limit: int = 5) -> tuple[Skill, ...]: ...
