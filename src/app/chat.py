import argparse
import os
import sys
from pathlib import Path

from src.adapters.models.litellm_model import create_model, load_model_config, model_labels
from src.client.terminal_chat import run_terminal_chat
from src.harness.tools.builtins import create_builtin_registry
from src.runtime.session.chat_session import ChatSession


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniCC 终端对话")
    parser.add_argument("--chat-only", action="store_true", help="只使用文字对话")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(os.environ.get("MINICC_MODELS_CONFIG") or project_root / "models.json")
    try:
        config = load_model_config(config_path)
        model_name = os.environ.get("MINICC_MODEL", "").strip() or config.get("default")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("请设置默认模型或通过 MINICC_MODEL 选择模型。")
        tools = None if args.chat_only else create_builtin_registry(Path.cwd())
        session = ChatSession(create_model(config, model_name), registry=tools)
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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
