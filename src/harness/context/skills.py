"""Small provider seam for loading task-relevant Skills into Context."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    content: str
    source: str


class SkillProvider(Protocol):
    def select(self, query: str, *, agent_id: str) -> tuple[Skill, ...]: ...
