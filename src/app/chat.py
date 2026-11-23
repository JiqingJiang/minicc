import argparse
import os
import sys
from pathlib import Path

from src.adapters.models.litellm_model import create_model, create_memory_embedder, load_model_config, model_labels
from src.client.terminal_chat import run_terminal_chat
from src.harness.tools.builtins import create_builtin_registry
from src.harness.context.assembler import ContextAssembler, ContextBudget, project_identity
from src.harness.context.prompt import PromptCatalog
from src.harness.memory.store import state_root
from src.harness.memory.manager import MemoryManager
from src.harness.memory.remember_tool import RememberMemoryTool, SearchMemoryTool
from src.harness.tools.registry import ToolRegistry
from src.runtime.session.chat_session import ChatSession


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniCC 终端对话")
    parser.add_argument("--chat-only", action="store_true", help="禁用工作区工具；保留长期记忆")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(os.environ.get("MINICC_MODELS_CONFIG") or project_root / "models.json")
    try:
        config = load_model_config(config_path)
        model_name = os.environ.get("MINICC_MODEL", "").strip() or config.get("default")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("请设置默认模型或通过 MINICC_MODEL 选择模型。")
        agent_id = config.get("agent_id", "default")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id 必须是非空字符串。")
        workspace = Path.cwd().resolve()
        project_id = project_identity(workspace)
        memory = MemoryManager(state_root(), project_id, create_memory_embedder(config))
        tools = ToolRegistry() if args.chat_only else create_builtin_registry(workspace)
        tools.register(RememberMemoryTool(memory))
        tools.register(SearchMemoryTool(memory))
        session = ChatSession(
            create_model(config, model_name),
            registry=tools,
            context=ContextAssembler(
                workspace,
                memory=memory,
                budget=ContextBudget.from_config(config.get("context_budget")),
                prompt_catalog=PromptCatalog.from_config(config.get("agent_profiles")),
                agent_id=agent_id,
            ),
            history_path=state_root() / "projects" / project_id / "session.json",
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    def select_model(alias: str) -> None:
        session.set_model(create_model(config, alias))

    run_terminal_chat(
        session,
        active_model=model_name,
        models=model_labels(config),
        select_model=select_model,
        memory=memory,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
