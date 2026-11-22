import json
import os
import sys
from collections.abc import Callable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from src.runtime.session.chat_session import ApprovalRequest, ChatSession, RetryRequest, TurnPause
from src.harness.tools.base import ToolCall, ToolResult


def _printable(text: str) -> str:
    return "".join(
        char if char.isprintable() or char in "\n\t" else char.encode("unicode_escape").decode("ascii")
        for char in text
    )


def _console(*, color: bool | None = None) -> Console:
    enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ if color is None else color
    return Console(
        file=sys.stdout,
        force_terminal=enabled,
        no_color=not enabled,
        highlight=False,
        soft_wrap=True,
    )


def _print_tool_start(call: ToolCall, arguments: dict, *, color: bool) -> None:
    params = json.dumps(arguments, ensure_ascii=False, indent=2)
    if color:
        _console(color=True).print(
            Panel(
                Syntax(params, "json", theme="ansi_dark", word_wrap=True),
                title=f"[Tool] {call.name}",
                title_align="left",
                border_style="cyan",
                padding=(0, 1),
            )
        )
    else:
        print(f"[Tool] {call.name}\n{params}", flush=True)


def _print_tool_result(call: ToolCall, result: ToolResult, *, color: bool) -> None:
    content = _printable(result.content)
    if result.error_code is None:
        if color:
            _console(color=True).print(
                Panel(
                    Text(content or "（空结果）"),
                    title=f"✓ {call.name} · 完成",
                    title_align="left",
                    border_style="green",
                    padding=(0, 1),
                )
            )
            return
        label = f"[Tool ✓] {call.name} 成功"
    else:
        if color:
            _console(color=True).print(
                Panel(
                    Text(content or "（空结果）"),
                    title=f"✗ {call.name} · {result.error_code}",
                    title_align="left",
                    border_style="red",
                    padding=(0, 1),
                )
            )
            return
        label = f"[Tool ✗] {call.name} 失败 [{result.error_code}]"
    print(label, flush=True)
    print("  " + (content.replace("\n", "\n  ") if content else "（空结果）"), flush=True)


def choose_model(models: dict[str, str], active_model: str) -> str:
    choices = {str(number): alias for number, alias in enumerate(models, start=1)}
    for number, alias in choices.items():
        current = "（当前）" if alias == active_model else ""
        print(f"  {number}. {models[alias]} [{alias}]{current}")
    print("切换后，新模型会收到当前聊天历史。")
    choice = input("输入编号或模型别名，回车返回 › ").strip()
    return choices.get(choice, choice)


def show_result(session: ChatSession, result: str | TurnPause) -> None:
    console = _console()
    rich = sys.stdout.isatty()
    while isinstance(result, ApprovalRequest):
        preview = _printable(result.preview)
        if rich:
            console.print(
                Panel(
                    Text(preview),
                    title="需要你的批准",
                    title_align="left",
                    border_style="yellow",
                    padding=(1, 2),
                )
            )
        else:
            print("\n╭─ 工具审批：请作出选择 ─╮")
            print(preview)
            print("╰──────────────────────╯")
        while True:
            if rich:
                console.print("[bold yellow]批准操作？[/] [green]yes[/] 执行 · [red]no[/] 拒绝 · 回车默认拒绝 · /cancel 结束", end="\n› ")
                answer = input().strip().lower()
            else:
                answer = input("选择 yes 批准 / no 拒绝（回车默认拒绝，/cancel 结束本轮） › ").strip().lower()
            if answer == "/cancel":
                session.cancel()
                print("当前轮次已结束。")
                return
            if answer in {"y", "yes", "1", "n", "no", "2", ""}:
                break
            print("请选择 yes/y/1 批准，或 no/n/2 拒绝。")
        result = session.answer_approval(answer in {"y", "yes", "1"})
    if isinstance(result, RetryRequest):
        if rich:
            Console(file=sys.stderr).print(
                Panel(
                    Text(f"{result.message}\n输入 /retry 从当前位置重试，或 /cancel 结束本轮。"),
                    title="本轮暂停",
                    border_style="yellow",
                )
            )
        else:
            print(f"{result.message} 输入 /retry 从当前位置重试，或 /cancel 结束本轮。", file=sys.stderr)
    else:
        if rich:
            console.print(
                Panel(
                    Markdown(_printable(result)),
                    title="MiniCC",
                    title_align="left",
                    border_style="bright_blue",
                    padding=(1, 2),
                )
            )
        else:
            print(f"MiniCC › {result}", flush=True)


def run_terminal_chat(
    session: ChatSession,
    *,
    active_model: str,
    models: dict[str, str],
    select_model: Callable[[str], None],
) -> None:
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    color = terminal and "NO_COLOR" not in os.environ
    session.set_tool_observer(lambda call, arguments: _print_tool_start(call, arguments, color=color))
    session.set_tool_result_observer(lambda call, result: _print_tool_result(call, result, color=color))
    if terminal:
        _console(color=color).print("[dim]输入 /model 选择供应商和模型 · /cancel 结束当前轮 · Ctrl+D 退出[/]")

    try:
        while True:
            if terminal:
                _console(color=color).print(f"\n[bold cyan]你[/] [dim][{active_model}][/] › ", end="")
                line = input()
            else:
                line = input()
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

            if line.strip().casefold() in {"y", "yes", "n", "no"}:
                print(
                    "当前没有待处理的工具审批；这次选择没有发送给模型，也没有批准任何操作。"
                    "请先让 Agent 发起 write、edit 或 bash 工具调用。",
                    file=sys.stderr,
                )
                continue

            try:
                if line.strip() == "/cancel":
                    print("当前轮次已结束。" if session.cancel() else "当前没有进行中的轮次。")
                    continue
                reply = session.retry() if line.strip() == "/retry" else session.send(line)
                show_result(session, reply)
            except (ValueError, RuntimeError, OSError) as error:
                if color:
                    Console(file=sys.stderr).print(
                        Panel(Text(str(error)), title="本轮失败", border_style="red")
                    )
                else:
                    print(f"本轮失败。{error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        session.cancel()
        if terminal:
            print()
