import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from src.runtime.session.chat_session import ApprovalRequest, ChatSession, RetryRequest, TurnPause
from src.harness.tools.base import ToolCall, ToolResult
from src.harness.context.assembler import project_identity
from src.harness.memory.manager import MemoryManager
from src.harness.memory.store import state_root
from src.runtime.task.state import TaskStateStore


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
    memory: MemoryManager | None = None,
) -> None:
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    color = terminal and "NO_COLOR" not in os.environ
    session.set_tool_observer(lambda call, arguments: _print_tool_start(call, arguments, color=color))
    session.set_tool_result_observer(lambda call, result: _print_tool_result(call, result, color=color))
    if terminal:
        _console(color=color).print("[dim]输入 /model 选择模型 · /new 开始新聊天 · /memory 管理记忆 · /task 管理任务状态 · /context 查看来源记录 · /cancel 结束当前轮 · Ctrl+D 退出[/]")

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

            if command[0] == "/memory":
                _manage_memory(line, memory or MemoryManager(state_root(), project_identity(Path.cwd())))
                continue

            if command[0] == "/task":
                _manage_task(line, Path.cwd())
                continue

            if command[0] == "/context":
                _show_context_trace()
                continue

            if command[0] == "/new":
                try:
                    session.new_chat()
                except (OSError, RuntimeError) as error:
                    print(str(error), file=sys.stderr)
                else:
                    print("已开始新聊天；之前的原始记录已归档，相关 Memory 仍可使用。", flush=True)
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


def _manage_memory(line: str, manager: MemoryManager) -> None:
    """Memory management remains user-facing; additions are made by the Agent."""
    parts = line.split(maxsplit=3)
    if len(parts) < 2 or parts[1] in {"help", "?"}:
        print("用法：/memory [global|project] list|search <词>|conflicts|resolve <ID>|history <ID>|edit <ID> <新内容>|delete <ID>")
        return
    if parts[1] in {"global", "project"}:
        scope = parts[1]
        action_parts = line.split(maxsplit=2)
        action_line = action_parts[2] if len(action_parts) > 2 else ""
    else:
        scope, action_line = "project", line[len("/memory"):].strip()
    args = action_line.split(maxsplit=2)
    action = args[0] if args else "list"
    try:
        if action == "list":
            items = manager.list(scope)
            for item in items:
                _print_memory(item)
            if not items:
                print("还没有这类记忆。")
        elif action == "search":
            query = action_line[len(action):].strip()
            hits = manager.search(query, scope=scope, limit=10)
            for hit in hits:
                _print_memory(hit.memory)
                print(f"  匹配度={hit.score:.3f}；依据={', '.join(hit.reasons)}")
            if not hits:
                print("没有匹配的记忆。")
        elif action == "edit":
            if len(args) < 3:
                raise ValueError("用法：/memory edit <ID> <新内容>")
            item = manager.update(scope, args[1], args[2])
            print(f"已更新记忆 {item.id[:8]}。")
        elif action == "history":
            if len(args) < 2:
                raise ValueError("用法：/memory history <ID>")
            item = manager.history(scope, args[1])
            _print_memory(item)
            for revision in item.history or []:
                print(f"  历史 [{revision.get('source_type')}] {revision.get('updated_at')} {revision.get('content', '')}")
        elif action == "delete":
            if len(args) < 2:
                raise ValueError("用法：/memory delete <ID>")
            manager.delete(scope, args[1])
            print("已删除记忆；它不会再进入 Context。")
        elif action == "conflicts":
            items = manager.conflicts(scope)
            for item in items:
                _print_memory(item)
                print(f"  冲突组={item.conflict_group}")
            if not items:
                print("没有待处理的冲突。")
        elif action == "resolve":
            if len(args) < 2:
                raise ValueError("用法：/memory resolve <保留的记忆ID>")
            item = manager.resolve(scope, args[1])
            print(f"已保留 {item.id[:8]}；同组其他候选已归档。")
        else:
            raise ValueError("不支持的操作。输入 /memory help 查看用法。")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"记忆操作失败：{error}", file=sys.stderr)


def _manage_task(line: str, workspace: Path) -> None:
    parts = line.split(maxsplit=2)
    identity = project_identity(workspace)
    store = TaskStateStore(state_root() / "projects" / identity / "task.json")
    action = parts[1] if len(parts) > 1 else "view"
    try:
        if action in {"help", "?"}:
            print("用法：/task view|goal <目标>|requirement|decision|progress|open <内容>|clear")
        elif action == "view":
            state = store.load()
            print(f"目标：{state.goal or '（未设置）'}")
            for title, values in (("已确认要求", state.requirements), ("已确认决定", state.decisions), ("进度", state.progress), ("待解决", state.open_questions)):
                print(f"{title}：" + ("；".join(values) if values else "（无）"))
        elif action == "goal":
            if len(parts) < 3:
                raise ValueError("用法：/task goal <目标>")
            store.set_goal(parts[2])
            print("已更新当前任务目标。")
        elif action == "clear":
            store.clear()
            print("已清除当前任务状态。")
        else:
            if len(parts) < 3:
                raise ValueError("请提供状态内容。输入 /task help 查看用法。")
            store.add(action, parts[2])
            print("已记录当前任务状态。")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"任务状态操作失败：{error}", file=sys.stderr)


def _show_context_trace() -> None:
    path = state_root() / "context-traces.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        identity = project_identity(Path.cwd())
        trace = next((json.loads(line) for line in reversed(lines) if json.loads(line).get("project_id") == identity), None)
    except (OSError, json.JSONDecodeError) as error:
        print(f"无法读取 Context 记录：{error}", file=sys.stderr)
        return
    if trace is None:
        print("还没有 Context 请求记录。")
        return
    print(f"估算输入：{trace.get('estimated_tokens')} tokens；实际输入：{trace.get('actual_input_tokens')}；实际输出：{trace.get('actual_output_tokens')}")
    print(f"裁剪消息：{trace.get('trimmed_messages')}；生成摘要：{'是' if trace.get('summarized') else '否'}；预算超出：{trace.get('budget_overflow_tokens', 0)} tokens")
    for source in trace.get("sources", []):
        print("- " + json.dumps(source, ensure_ascii=False))


def _print_memory(item) -> None:
    print(f"{item.id[:8]} [{item.scope}/{item.category}] 来源={item.source_type} 创建={item.created_at} 更新={item.updated_at} 历史版本={len(item.history or [])} {item.content}")
