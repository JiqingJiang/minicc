import sys
from collections.abc import Callable

from src.runtime.session.chat_session import ChatSession


def choose_model(models: dict[str, str], active_model: str) -> str:
    choices = {str(number): alias for number, alias in enumerate(models, start=1)}
    for number, alias in choices.items():
        current = "（当前）" if alias == active_model else ""
        print(f"  {number}. {models[alias]} [{alias}]{current}")
    print("切换后，新模型会收到当前聊天历史。")
    choice = input("输入编号或模型别名，回车返回 › ").strip()
    return choices.get(choice, choice)


def run_terminal_chat(
    session: ChatSession,
    *,
    active_model: str,
    models: dict[str, str],
    select_model: Callable[[str], None],
) -> None:
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    if terminal:
        print("输入 /model 选择供应商和模型。")

    try:
        while True:
            line = input(f"你 [{active_model}] › " if terminal else "")
            command = line.split(maxsplit=1)
            if not command:
                continue

            if command[0] == "/model":
                alias = command[1].strip() if len(command) == 2 else choose_model(models, active_model)
                if not alias or alias == active_model:
                    continue
                try:
                    select_model(alias)
                except (ValueError, RuntimeError) as error:
                    print(f"切换失败。{error}", file=sys.stderr)
                else:
                    active_model = alias
                    print(f"已切换到 {models[alias]} [{alias}]。", flush=True)
                continue

            try:
                reply = session.send(line)
            except (ValueError, RuntimeError, OSError) as error:
                print(f"本轮失败。{error}", file=sys.stderr)
            else:
                print(f"MiniCC › {reply}", flush=True)
    except (EOFError, KeyboardInterrupt):
        if terminal:
            print()
