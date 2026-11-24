"""Application entry point wiring models, tools, memory, skills, MCP, subagents, and session."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from src.adapters.mcp.client import McpManager
from src.adapters.models.litellm_model import (
    create_memory_embedder,
    create_model,
    load_model_config,
    model_labels,
)
from src.client.terminal_chat import run_terminal_chat
from src.harness.context.assembler import ContextAssembler, ContextBudget, project_identity
from src.harness.context.prompt import PromptCatalog
from src.harness.memory.manager import MemoryManager
from src.harness.memory.remember_tool import RememberMemoryTool, SearchMemoryTool
from src.harness.memory.store import migrate_legacy_state, state_root
from src.harness.orchestration.subagents.runner import DelegateSubAgentTool, SubAgentRunner
from src.harness.skills import (
    InstallSkillTool,
    LoadSkillTool,
    SearchSkillsTool,
    SkillRegistry,
    SkillsCatalogClient,
)
from src.harness.tools.builtins import create_builtin_registry
from src.harness.tools.registry import ToolRegistry
from src.runtime.hitl.contracts import AskUserTool, UpdatePlanTool
from src.runtime.session.chat_session import ChatSession
from src.runtime.task.state import TaskStateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniCC 终端对话")
    parser.add_argument("--chat-only", action="store_true", help="禁用工作区工具；保留长期记忆")
    parser.add_argument(
        "--mode",
        choices=("normal", "plan", "goal"),
        default="normal",
        help="启动时的初始运行模式（normal | plan | goal）",
    )
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(os.environ.get("MINICC_MODELS_CONFIG") or project_root / "models.json")
    mcp_manager: McpManager | None = None
    try:
        config = load_model_config(config_path)
        model_name = os.environ.get("MINICC_MODEL", "").strip() or config.get("default")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("请设置默认模型或通过 MINICC_MODEL 选择模型。")
        agent_id = config.get("agent_id", "default")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id 必须是非空字符串。")
        migrate_legacy_state()
        workspace = Path.cwd().resolve()
        project_id = project_identity(workspace)
        project_state_dir = state_root() / "projects" / project_id
        task_store = TaskStateStore(project_state_dir / "task.json")
        memory = MemoryManager(state_root(), project_id, create_memory_embedder(config))

        skill_config = config.get("skills", {})
        if not isinstance(skill_config, dict):
            raise ValueError("skills 配置必须是对象。")
        skills_enabled = skill_config.get("enabled", True)
        if not isinstance(skills_enabled, bool):
            raise ValueError("skills.enabled 必须是布尔值。")
        skills = SkillRegistry(
            workspace,
            skills_root=state_root() / "skills",
            state_path=state_root() / "skills.json",
            embedder=memory.embedder,
            feature_enabled=skills_enabled,
        )
        catalog = SkillsCatalogClient()
        tools = ToolRegistry() if args.chat_only else create_builtin_registry(workspace)
        tools.bind_state_path(state_root() / "tools.json")
        tools.register(RememberMemoryTool(memory))
        tools.register(SearchMemoryTool(memory))
        tools.register(SearchSkillsTool(catalog))
        if not args.chat_only:
            tools.register(InstallSkillTool(catalog, skills))
        tools.register(LoadSkillTool(skills))
        tools.register(AskUserTool())
        tools.register(UpdatePlanTool(task_store))

        if not args.chat_only:
            mcp_manager = McpManager.from_config(
                config.get("mcp_servers"),
                workspace=workspace,
                state_path=state_root() / "mcp.json",
            )
            mcp_manager.register_tools(tools)

        session = ChatSession(
            create_model(config, model_name),
            registry=tools,
            context=ContextAssembler(
                workspace,
                memory=memory,
                budget=ContextBudget.from_config(config.get("context_budget")),
                prompt_catalog=PromptCatalog.from_config(config.get("agent_profiles")),
                skill_provider=skills,
                agent_id=agent_id,
                task_store=task_store,
            ),
            history_path=project_state_dir / "session.json",
            checkpoint_path=project_state_dir / "checkpoint.json",
            mode=args.mode,
        )

        if not args.chat_only:
            subagent_runner = SubAgentRunner(
                complete_getter=session.get_model,
                registry=tools,
                approval_policy_getter=session.get_approval_policy,
                mode_getter=lambda: session.mode,
            )
            tools.register(DelegateSubAgentTool(subagent_runner))
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    def select_model(alias: str) -> None:
        session.set_model(create_model(config, alias))

    try:
        run_terminal_chat(
            session,
            active_model=model_name,
            models=model_labels(config),
            select_model=select_model,
            memory=memory,
            skill_registry=skills,
            tool_registry=tools,
            mcp_manager=mcp_manager,
        )
    finally:
        if mcp_manager is not None:
            mcp_manager.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
