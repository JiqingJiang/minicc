"""Model-facing loader and catalog installer for Skill instructions and package resources."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json

from src.harness.skills.marketplace import SkillsCatalogClient
from src.harness.skills.registry import SkillRegistry
from src.harness.tools.base import ApprovalAction, BaseTool, ToolFailure

_MAX_SKILL_BODY_CHARS = 7_000
_MAX_SKILL_RESOURCE_CHARS = 7_200


class SearchSkillsTool(BaseTool):
    name = "search_skills"
    description = "Search the online Skills catalog when the user asks to find a Skill to install."
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 300},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, catalog: SkillsCatalogClient) -> None:
        self.catalog = catalog

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            results = self.catalog.search(arguments["query"], limit=arguments.get("limit", 5))
        except (OSError, RuntimeError, ValueError) as error:
            raise ToolFailure("SKILL_SEARCH_FAILED", str(error)[:400]) from error
        checkpoint()
        if not results:
            return "没有找到匹配的 Skill。可以换一种能力描述重新搜索。"
        return json.dumps(
            [
                {
                    "id": result.id,
                    "name": result.name,
                    "description": result.description,
                    "source": result.source,
                    "verified_publisher": result.verified,
                    "compatible_agents": result.agents,
                    "category": result.category,
                    "average_rating": result.avg_rating,
                    "rating_count": result.rating_count,
                    "url": result.url,
                }
                for result in results
            ],
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class _InstallSkillAction:
    catalog: SkillsCatalogClient
    registry: SkillRegistry
    skill_id: str
    preview: str
    elevated: bool = False

    def execute(self, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            files = self.catalog.fetch(self.skill_id)
            checkpoint()
            skill = self.registry.install_package(self.skill_id, files)
        except FileExistsError as error:
            raise ToolFailure("SKILL_ALREADY_INSTALLED", str(error)) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise ToolFailure("SKILL_INSTALL_FAILED", str(error)[:400]) from error
        checkpoint()
        return (
            f"已安装 {skill.name}（来源：{skill.source}）并注册到 MiniCC。"
            "现在可以调用 load_skill 读取它的指令。"
        )


class InstallSkillTool(BaseTool):
    name = "install_skill"
    description = "Download a Skill selected from search_skills into MiniCC's private Skill library and register it."
    requires_approval = True
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "string", "minLength": 3, "maxLength": 256},
        },
        "required": ["skill_id"],
        "additionalProperties": False,
    }

    def __init__(self, catalog: SkillsCatalogClient, registry: SkillRegistry) -> None:
        self.catalog = catalog
        self.registry = registry

    def approval(self, arguments: dict, checkpoint: Callable[[], None]) -> ApprovalAction:
        checkpoint()
        skill_id = arguments["skill_id"].strip()
        preview = (
            f"从公开目录 skillmd.com 下载并安装 Skill：{skill_id}\n"
            f"目标目录：{self.registry.skills_root}"
        )
        return _InstallSkillAction(self.catalog, self.registry, skill_id, preview)

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        action = self.approval(arguments, checkpoint)
        return action.execute(checkpoint)


class LoadSkillTool(BaseTool):
    name = "load_skill"
    description = (
        "Load the full instructions for a skill from the available skills catalog. "
        "Use file_path to read a referenced text file inside that skill package when needed."
    )
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Exact skill name from the available skills catalog.",
            },
            "file_path": {
                "type": "string",
                "minLength": 1,
                "description": "Optional relative path to a text resource inside the skill package.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def execute(self, arguments: dict, checkpoint: Callable[[], None]) -> str:
        checkpoint()
        try:
            skill = self.registry.load(arguments["name"], resource_path=arguments.get("file_path"))
        except (OSError, RuntimeError, ValueError) as error:
            raise ToolFailure("SKILL_RESOURCE_DENIED", str(error)) from error
        checkpoint()
        if skill is None or skill.content is None:
            raise ToolFailure(
                "SKILL_NOT_AVAILABLE",
                "Skill does not exist, is disabled, or cannot be invoked by the model.",
            )
        if arguments.get("file_path"):
            body = skill.content
            if len(body) > _MAX_SKILL_RESOURCE_CHARS:
                body = body[:_MAX_SKILL_RESOURCE_CHARS] + "\n[...Skill 资源内容已按预算截断...]"
            return f'<skill_resource name="{skill.name}" path="{arguments["file_path"]}">\n{body}\n</skill_resource>'
        body = skill.content
        if len(body) > _MAX_SKILL_BODY_CHARS:
            body = (
                body[:_MAX_SKILL_BODY_CHARS]
                + "\n[Instruction text truncated. Load a referenced resource by file_path if more detail is needed.]"
            )
        return (
            f'<skill_content name="{skill.name}">\n'
            "<skill_instructions>\n"
            f"{body}\n"
            "</skill_instructions>\n"
            "<skill_resources>\n"
            "If these instructions reference files under references/, assets/, or scripts/, "
            "load readable text with load_skill(name, file_path='relative/path'). "
            "Only files inside this skill package can be loaded.\n"
            "</skill_resources>\n"
            "</skill_content>"
        )
