"""Terminal client with Claude Code-style framed prompt, persistent bottom status bar, collapsible tool logs, and zero-pollution TUI menus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from src.adapters.mcp.client import McpManager
from src.client.tui_selector import (
    MenuItem,
    MenuOutcome,
    SlashCommandCompleter,
    TUI_STYLE,
    is_interactive_tty,
    run_interactive_menu,
    transient_input,
)
from src.harness.agent_loop.run_turn import ApprovalRequest, ClarificationRequest, RetryRequest, TurnPause
from src.harness.context.assembler import project_identity
from src.harness.memory.manager import MemoryManager
from src.harness.memory.store import Memory, state_root
from src.harness.skills import SkillRegistry
from src.harness.tools.base import ToolCall, ToolResult
from src.harness.tools.registry import ToolRegistry
from src.runtime.session.chat_session import ChatSession
from src.runtime.task.state import TaskStateStore


class PermissionModeController:
    """Interactive, per-process approval mode matching PaiCLI's Default/Auto flow."""

    def __init__(self) -> None:
        self.mode = "default"

    @property
    def auto_approve(self) -> bool:
        return self.mode == "auto"

    def set(self, mode: str) -> str:
        if mode not in {"default", "auto"}:
            raise ValueError("权限模式必须是 default 或 auto。")
        self.mode = mode
        return self.mode

    def toggle(self) -> str:
        return self.set("auto" if self.mode == "default" else "default")

    def approval_policy(self) -> str:
        return "approve" if self.auto_approve else "ask"


@dataclass
class ToolLogEntry:
    call: ToolCall
    arguments: dict
    result: ToolResult | None = None


class ToolDisplayController:
    """Manage default-collapsed tool call summaries with Ctrl+O / /expand detail view."""

    def __init__(self, *, expanded: bool = False) -> None:
        self.expanded = expanded
        self.entries: list[ToolLogEntry] = []

    def toggle(self) -> bool:
        self.expanded = not self.expanded
        return self.expanded

    def record_start(self, call: ToolCall, arguments: dict) -> ToolLogEntry:
        entry = ToolLogEntry(call=call, arguments=dict(arguments))
        self.entries.append(entry)
        return entry

    def record_result(self, call: ToolCall, result: ToolResult) -> ToolLogEntry:
        for entry in reversed(self.entries):
            if entry.call.call_id == call.call_id:
                entry.result = result
                return entry
        entry = ToolLogEntry(call=call, arguments={}, result=result)
        self.entries.append(entry)
        return entry


@contextmanager
def _macos_passthrough_ctrl_keys():
    """Disable macOS BSD VDISCARD (^O) and VDSUSP (^Y) so Ctrl+O reaches prompt_toolkit."""
    if not sys.stdin.isatty():
        yield
        return
    try:
        import termios

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        patched = termios.tcgetattr(fd)
        cc = list(patched[6])
        vdiscard = getattr(termios, "VDISCARD", None)
        vdsusp = getattr(termios, "VDSUSP", None)
        if vdiscard is not None and vdiscard < len(cc):
            cc[vdiscard] = 0
        if vdsusp is not None and vdsusp < len(cc):
            cc[vdsusp] = 0
        patched[6] = cc
        termios.tcsetattr(fd, termios.TCSANOW, patched)
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, original)
        except Exception:
            pass


def _format_model_display(active_model: str, models: dict[str, str] | None = None) -> str:
    """Resolve full model name (e.g. '智谱 GLM / glm-5.3-flash [glm]') instead of just provider alias."""
    if not active_model:
        return ""
    if not models or active_model not in models:
        return active_model
    raw_label = models[active_model].replace("（缺少密钥）", "").strip()
    if raw_label and raw_label != active_model:
        return f"{raw_label} [{active_model}]"
    return active_model


class TerminalInput:
    """Claude Code-style framed input box with a persistent bottom status bar."""

    def __init__(
        self,
        permission_mode: PermissionModeController,
        active_model: str = "",
        *,
        models: dict[str, str] | None = None,
        mode_getter: Callable[[], str] | None = None,
        tool_display: ToolDisplayController | None = None,
        on_toggle_expand: Callable[[], None] | None = None,
    ) -> None:
        self.permission_mode = permission_mode
        self.active_model = active_model
        self.models = models or {}
        self.mode_getter = mode_getter
        self.tool_display = tool_display
        self.on_toggle_expand = on_toggle_expand
        self.transient_notice: str = ""
        bindings = KeyBindings()

        @bindings.add(Keys.BackTab)
        def _toggle_permission_mode(event) -> None:
            self.permission_mode.toggle()
            event.app.invalidate()

        @bindings.add("c-o")
        def _toggle_tool_expand(event) -> None:
            if self.tool_display is not None:
                is_exp = self.tool_display.toggle()
                self.transient_notice = f"工具详情已{'展开' if is_exp else '折叠'}"
                if is_exp and self.tool_display.entries:
                    last_entry = self.tool_display.entries[-1]

                    def _render_latest() -> None:
                        color_on = sys.stdout.isatty() and "NO_COLOR" not in os.environ
                        _print_tool_start(last_entry.call, last_entry.arguments, color=color_on, display=None)
                        if last_entry.result is not None:
                            _print_tool_result(last_entry.call, last_entry.result, color=color_on, display=None)

                    run_in_terminal(_render_latest)
            if self.on_toggle_expand is not None:
                self.on_toggle_expand()
            event.app.invalidate()

        self._session = PromptSession(
            message=self._prompt_message,
            bottom_toolbar=self._bottom_toolbar,
            key_bindings=bindings,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
            style=TUI_STYLE,
        )

    @property
    def model_display(self) -> str:
        return _format_model_display(self.active_model, self.models)

    def _box_width(self) -> int:
        cols = shutil.get_terminal_size((80, 24)).columns
        return max(40, min(cols - 2, 100))

    def _prompt_message(self) -> FormattedText:
        width = self._box_width()
        top_border = "╭" + "─" * (width - 2) + "╮\n"
        return FormattedText(
            [
                ("class:prompt.box", top_border),
                ("class:prompt.box", "│ "),
                ("class:prompt.arrow", "› "),
            ]
        )

    def _bottom_toolbar(self) -> FormattedText:
        perm_style = "class:toolbar.perm.auto" if self.permission_mode.auto_approve else "class:toolbar.perm.default"
        perm_label = "⚡ Auto（免审批）" if self.permission_mode.auto_approve else "🛡️ Default（逐项审批）"
        run_mode = self.mode_getter() if self.mode_getter is not None else "normal"
        mode_style = f"class:toolbar.mode.{run_mode}"
        expanded = self.tool_display.expanded if self.tool_display is not None else False
        tool_state = "展开" if expanded else "折叠"

        fragments: list[tuple[str, str]] = [
            (perm_style, f" {perm_label} "),
            ("class:toolbar.sep", "│"),
            (mode_style, f" 模式: {run_mode.upper()} "),
            ("class:toolbar.sep", "│"),
            ("class:toolbar.model", f" 模型: {self.model_display or '未选择'} "),
            ("class:toolbar.sep", "│"),
            ("class:toolbar.tools", f" 工具详情: {tool_state} (Ctrl+O / /expand) "),
            ("class:toolbar.sep", "│"),
            ("class:toolbar.tools", " Shift+Tab 切权限 · / 命令菜单 "),
        ]
        if self.transient_notice:
            fragments.extend(
                [
                    ("class:toolbar.sep", "│"),
                    ("class:status", f" ✦ {self.transient_notice} "),
                ]
            )
        return FormattedText(fragments)

    def read(self, prompt: str | None = None) -> str:
        with _macos_passthrough_ctrl_keys():
            if prompt is None:
                line = self._session.prompt()
                self.transient_notice = ""
                return line
            return self._session.prompt(
                message=FormattedText(
                    [
                        ("class:prompt.box", "│ "),
                        ("class:title", f"{prompt} › "),
                    ]
                )
            )


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


def _ask_prompt(prompt_text: str, read_input: Callable[[str], str] | None = None) -> str:
    if read_input is not None:
        return read_input(prompt_text).strip()
    return input(f"{prompt_text} › ").strip()


def _use_tui_menu(read_input: Callable[[str], str] | None) -> bool:
    if not is_interactive_tty():
        return False
    if read_input is not None and getattr(read_input, "__name__", "") != "read":
        return False
    return True


def _show_welcome(model_display: str, mode: str = "normal") -> None:
    console = _console()
    logo = (
        "███╗   ███╗██╗███╗   ██╗██╗ ██████╗ ██████╗\n"
        "████╗ ████║██║████╗  ██║██║██╔════╝██╔════╝\n"
        "██╔████╔██║██║██╔██╗ ██║██║██║     ██║\n"
        "██║╚██╔╝██║██║██║╚██╗██║██║██║     ██║\n"
        "██║ ╚═╝ ██║██║██║ ╚████║██║╚██████╗╚██████╗\n"
        "╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝╚═╝ ╚═════╝ ╚═════╝"
    )
    console.print(
        Panel(
            f"[bold #38bdf8]{logo}[/]\n"
            f"[bold #e2e8f0]MiniCC Agent Harness[/] [dim]·[/] "
            f"[#38bdf8]{model_display}[/] [dim]·[/] "
            f"[#a78bfa]模式: {mode.upper()}[/] [dim]·[/] "
            f"[#94a3b8]{Path.cwd()}[/]\n"
            "[dim]输入 / 弹出命令菜单（退出自动擦除不污染对话） · Ctrl+O 展开/折叠工具详情 · Shift+Tab 切换审批模式[/]",
            border_style="#1e293b",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )


def _summarize_tool_args(arguments: dict) -> str:
    if not arguments:
        return ""
    for key in ("path", "url", "command", "query", "pattern", "skill_id", "name", "question", "goal", "task"):
        if key in arguments and isinstance(arguments[key], str):
            val = " ".join(arguments[key].split())
            if len(val) > 48:
                val = val[:45] + "..."
            return f'{key}="{val}"'
    raw = json.dumps(arguments, ensure_ascii=False)
    return raw[:48] + ("..." if len(raw) > 48 else "")


def _summarize_tool_output(result: ToolResult) -> str:
    content = _printable(result.content).strip()
    if not content:
        return "空结果"
    lines = content.splitlines()
    first = " ".join(lines[0].split())
    if len(first) > 52:
        first = first[:49] + "..."
    if len(lines) > 1:
        return f"{first} (共 {len(lines)} 行)"
    return first


def _print_tool_start(
    call: ToolCall,
    arguments: dict,
    *,
    color: bool,
    display: ToolDisplayController | None = None,
    record: bool = True,
) -> None:
    if display is not None and record:
        display.record_start(call, arguments)
    expanded = display.expanded if display is not None else True
    if color and not expanded:
        # In collapsed mode we wait for _print_tool_result to print a single compact summary line
        return
    params = json.dumps(arguments, ensure_ascii=False, indent=2)
    if color:
        _console(color=True).print(
            Panel(
                Syntax(params, "json", theme="ansi_dark", word_wrap=True),
                title=f"[bold #38bdf8]▾ ⚙ {call.name}[/]",
                title_align="left",
                border_style="#0284c7",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
    else:
        print(f"[Tool] {call.name}\n{params}", flush=True)


def _print_tool_result(
    call: ToolCall,
    result: ToolResult,
    *,
    color: bool,
    display: ToolDisplayController | None = None,
    record: bool = True,
) -> None:
    if display is not None and record:
        entry = display.record_result(call, result)
    else:
        entry = ToolLogEntry(call, {}, result)
    expanded = display.expanded if display is not None else True
    content = _printable(result.content)

    if color and not expanded:
        arg_summary = _summarize_tool_args(entry.arguments)
        out_summary = _summarize_tool_output(result)
        call_repr = f"{call.name}({arg_summary})" if arg_summary else call.name
        if result.error_code is None:
            _console(color=True).print(
                f"  [bold #38bdf8]▸ ⚙ {call_repr}[/] [bold #22c55e]✓ 完成[/] [dim]· {out_summary} (Ctrl+O 展开)[/]"
            )
        else:
            _console(color=True).print(
                f"  [bold #ef4444]▸ ✗ {call_repr} [{result.error_code}][/] [#fca5a5]· {out_summary} (Ctrl+O 展开)[/]"
            )
        return

    if result.error_code is None:
        if color:
            _console(color=True).print(
                Panel(
                    Text(content or "（空结果）", style="#e2e8f0"),
                    title=f"[bold #22c55e]▾ ✓ {call.name} · 完成[/]",
                    title_align="left",
                    border_style="#16a34a",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
            return
        label = f"[Tool ✓] {call.name} 成功"
    else:
        if color:
            _console(color=True).print(
                Panel(
                    Text(content or "（空结果）", style="#fca5a5"),
                    title=f"[bold #ef4444]▾ ✗ {call.name} · {result.error_code}[/]",
                    title_align="left",
                    border_style="#dc2626",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
            return
        label = f"[Tool ✗] {call.name} 失败 [{result.error_code}]"
    print(label, flush=True)
    print("  " + (content.replace("\n", "\n  ") if content else "（空结果）"), flush=True)


def _show_tool_inspector(display: ToolDisplayController, *, color: bool) -> None:
    """Show an erasable interactive TUI inspector for recent tool calls or print expanded details."""
    if not display.entries:
        if not is_interactive_tty():
            print("当前还没有工具调用记录。")
        return
    if is_interactive_tty():
        def build_items() -> list[MenuItem]:
            items: list[MenuItem] = []
            for idx, entry in enumerate(display.entries, 1):
                arg_sum = _summarize_tool_args(entry.arguments)
                res = entry.result
                status_tag = "✓ 成功" if (res and res.error_code is None) else (f"✗ {res.error_code}" if res else "运行中")
                out_sum = _summarize_tool_output(res) if res else ""
                items.append(
                    MenuItem(
                        key=str(idx),
                        label=f"{entry.call.name}({arg_sum})",
                        tag=status_tag,
                        meta=out_sum,
                        payload=entry,
                    )
                )
            return items

        def on_action(action: str, item: MenuItem | None, _idx: int) -> str | MenuOutcome | None:
            if action in {"t", "0"}:
                state = display.toggle()
                return f"默认工具显示已切换为：{'展开详细面板' if state else '单行折叠摘要'}"
            if item is None:
                return None
            if action in {"enter", "space", "v"}:
                entry: ToolLogEntry = item.payload
                params = json.dumps(entry.arguments, ensure_ascii=False, indent=2)
                out = _printable(entry.result.content) if entry.result else "（尚未返回结果）"
                detail = f"【工具】{entry.call.name}\n【参数】\n{params}\n\n【输出】\n{out}"
                return f"__DETAIL__:{detail}"
            return "按 Enter 查看该次调用完整参数与输出 · t 切换全局默认展开/折叠 · Esc 退出"

        run_interactive_menu(
            title=f"工具调用详情查看器 (/expand) · 默认模式: {'展开' if display.expanded else '折叠'}",
            subtitle="退出后自动擦除不污染聊天界面",
            items_provider=build_items,
            footer_hints=(
                ("↑/↓/鼠标", "选择调用"),
                ("Enter", "展开/折叠该条详情"),
                ("t", "切换全局默认展开/折叠"),
                ("Esc/q", "关闭面板"),
            ),
            on_key_action=on_action,
            initial_index=max(0, len(display.entries) - 1),
            erase_when_done=True,
        )
        return

    # Non-TTY fallback: toggle and print latest tool entry
    is_exp = display.toggle()
    print(f"工具详情显示已切换为：{'展开' if is_exp else '折叠'}。")
    if is_exp and display.entries:
        last = display.entries[-1]
        _print_tool_start(last.call, last.arguments, color=color, display=None)
        if last.result is not None:
            _print_tool_result(last.call, last.result, color=color, display=None)


def choose_model(
    models: dict[str, str], active_model: str, *, read_input: Callable[[str], str] | None = None,
) -> str:
    if _use_tui_menu(read_input):
        aliases = list(models.keys())
        init_idx = aliases.index(active_model) if active_model in aliases else 0
        outcome = run_interactive_menu(
            title="选择对话模型 (/model)",
            subtitle="切换后保留当前聊天历史 · 退出自动擦除",
            items_provider=lambda: [
                MenuItem(
                    key=alias,
                    label=f"{models[alias]}",
                    tag="当前" if alias == active_model else alias,
                    payload=alias,
                )
                for alias in aliases
            ],
            footer_hints=(("↑/↓/鼠标", "选择"), ("Enter", "确认切换"), ("Esc/q", "返回")),
            initial_index=init_idx,
            erase_when_done=True,
        )
        return outcome.item.key if outcome and outcome.item else ""

    choices = {str(number): alias for number, alias in enumerate(models, start=1)}
    for number, alias in choices.items():
        current = "（当前）" if alias == active_model else ""
        print(f"  {number}. {models[alias]} [{alias}]{current}")
    print("切换后，新模型会收到当前聊天历史。")
    choice = _ask_prompt("输入编号或模型别名，回车返回", read_input)
    return choices.get(choice, choice)


def show_result(
    session: ChatSession,
    result: str | TurnPause,
    *,
    permission_mode: PermissionModeController | None = None,
    read_input: Callable[[str], str] | None = None,
) -> None:
    console = _console()
    rich = sys.stdout.isatty()
    while isinstance(result, (ApprovalRequest, ClarificationRequest)):
        if isinstance(result, ApprovalRequest):
            preview = _printable(result.preview)
            title = "🚨 【高危提权审批】需要你的明确批准" if result.elevated else "🛡️ 需要你的批准"
            border = "#ef4444" if result.elevated else "#f59e0b"

            if _use_tui_menu(read_input):
                menu_items = [
                    MenuItem("yes", "✓ 批准并执行本次操作", tag="Yes"),
                    MenuItem("no", "✗ 拒绝本次操作", tag="No"),
                ]
                if not result.elevated and permission_mode is not None:
                    menu_items.append(
                        MenuItem("auto", "⚡ 批准并切换本会话为 Auto（后续沙箱内免审）", tag="Auto")
                    )
                menu_items.append(MenuItem("cancel", "⏹ 结束当前轮次 (/cancel)", tag="Cancel"))
                outcome = run_interactive_menu(
                    title=title,
                    subtitle=preview.replace("\n", " ↵ ")[:90],
                    items_provider=lambda: menu_items,
                    footer_hints=(("↑/↓/鼠标", "移动"), ("Enter", "确认决定"), ("v", "查看完整预览"), ("Esc", "默认拒绝")),
                    on_key_action=lambda act, it, ix: f"__DETAIL__:{preview}" if act == "v" else MenuOutcome(act, ix, it),
                    erase_when_done=True,
                )
                choice_key = outcome.item.key if outcome and outcome.item else "no"
                if choice_key == "cancel":
                    session.cancel()
                    print("当前轮次已结束。")
                    return
                if choice_key == "auto" and permission_mode is not None:
                    permission_mode.set("auto")
                    result = session.answer_approval(True)
                else:
                    result = session.answer_approval(choice_key == "yes")
                continue

            if rich:
                console.print(
                    Panel(
                        Text(preview, style="#f8fafc"),
                        title=f"[bold]{title}[/]",
                        title_align="left",
                        border_style=border,
                        box=box.ROUNDED,
                        padding=(1, 2),
                    )
                )
            else:
                print("\n╭─ 工具审批：请作出选择 ─╮")
                print(preview)
                print("╰──────────────────────╯")

            while True:
                if read_input is not None:
                    answer = read_input("批准操作？ yes 执行 · no 拒绝 · a 切换 Auto 并执行 · /cancel 结束")
                elif rich:
                    console.print(
                        "[bold yellow]批准操作？[/] [green]yes[/] 执行 · [red]no[/] 拒绝 · 回车默认拒绝 · /cancel 结束",
                        end="\n› ",
                    )
                    answer = input().strip().lower()
                else:
                    answer = input("选择 yes 批准 / no 拒绝（回车默认拒绝，/cancel 结束本轮） › ").strip().lower()
                answer = answer.strip().lower()
                if answer == "/cancel":
                    session.cancel()
                    print("当前轮次已结束。")
                    return
                if answer in {"a", "auto"} and permission_mode is not None:
                    permission_mode.set("auto")
                    result = session.answer_approval(True)
                    break
                if answer in {"y", "yes", "1", "n", "no", "2", ""}:
                    result = session.answer_approval(answer in {"y", "yes", "1"})
                    break
                print("请选择 yes/y/1 批准、no/n/2 拒绝，或 a 切换 Auto 并执行。")
        else:
            question = _printable(result.question)
            options_lines = [f"  {idx}. {opt}" for idx, opt in enumerate(result.options, start=1)]
            body = question + ("\n\n候选选项：\n" + "\n".join(options_lines) if options_lines else "")

            if _use_tui_menu(read_input) and result.options:
                c_items = [MenuItem(opt, opt, tag=f"选项 {i}") for i, opt in enumerate(result.options, 1)]
                if result.allow_custom:
                    c_items.append(MenuItem("__custom__", "✎ 输入自定义回答...", tag="自定义"))
                c_items.append(MenuItem("__cancel__", "⏹ 结束当前轮次 (/cancel)", tag="取消"))
                outcome = run_interactive_menu(
                    title=f"💬 澄清问题：{question}",
                    subtitle="↑/↓ 或鼠标点击选中 · 回答后自动收起",
                    items_provider=lambda: c_items,
                    footer_hints=(("↑/↓/鼠标", "选择"), ("Enter", "确认")),
                    erase_when_done=True,
                )
                if outcome is None or outcome.item is None or outcome.item.key == "__cancel__":
                    session.cancel()
                    print("当前轮次已结束。")
                    return
                if outcome.item.key == "__custom__":
                    custom_ans = transient_input("请输入自定义回答")
                    if not custom_ans:
                        session.cancel()
                        return
                    result = session.answer_clarification(custom_ans)
                else:
                    result = session.answer_clarification(outcome.item.key)
                continue

            if rich:
                console.print(
                    Panel(
                        Text(body, style="#f8fafc"),
                        title="[bold #a78bfa]💬 需要你的澄清回答[/]",
                        title_align="left",
                        border_style="#a78bfa",
                        box=box.ROUNDED,
                        padding=(1, 2),
                    )
                )
            else:
                print("\n╭─ 澄清问题 ─╮")
                print(body)
                print("╰────────────╯")

            while True:
                prompt_text = "输入选项编号或自定义回答（/cancel 结束本轮）"
                raw_ans = _ask_prompt(prompt_text, read_input)
                if raw_ans.lower() == "/cancel":
                    session.cancel()
                    print("当前轮次已结束。")
                    return
                if raw_ans.isdigit() and 1 <= int(raw_ans) <= len(result.options):
                    chosen = result.options[int(raw_ans) - 1]
                    result = session.answer_clarification(chosen)
                    break
                if raw_ans and (result.allow_custom or not result.options):
                    result = session.answer_clarification(raw_ans)
                    break
                print("请输入有效的选项编号或回答内容。")

    if isinstance(result, RetryRequest):
        if rich:
            Console(file=sys.stderr).print(
                Panel(
                    Text(f"{result.message}\n输入 /retry 从当前位置重试，或 /cancel 结束本轮。"),
                    title="[bold #f59e0b]⏸ 本轮暂停[/]",
                    border_style="#f59e0b",
                    box=box.ROUNDED,
                )
            )
        else:
            print(f"{result.message} 输入 /retry 从当前位置重试，或 /cancel 结束本轮。", file=sys.stderr)
    else:
        if rich:
            console.print(
                Panel(
                    Markdown(_printable(result)),
                    title="[bold #38bdf8]✦ MiniCC[/]",
                    title_align="left",
                    border_style="#3b82f6",
                    box=box.ROUNDED,
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
    skill_registry: SkillRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    mcp_manager: McpManager | None = None,
) -> None:
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    color = terminal and "NO_COLOR" not in os.environ
    permission_mode = PermissionModeController()
    tool_display = ToolDisplayController(expanded=False)
    terminal_input = (
        TerminalInput(
            permission_mode,
            active_model,
            models=models,
            mode_getter=lambda: session.mode,
            tool_display=tool_display,
        )
        if terminal
        else None
    )
    read_fn = terminal_input.read if terminal_input is not None else None
    tools = tool_registry if tool_registry is not None else session.registry
    session.set_approval_policy(
        permission_mode.approval_policy if terminal else lambda: "deny"
    )
    session.set_tool_observer(
        lambda call, arguments: _print_tool_start(call, arguments, color=color, display=tool_display)
    )
    session.set_tool_result_observer(
        lambda call, result: _print_tool_result(call, result, color=color, display=tool_display)
    )
    if terminal:
        _show_welcome(_format_model_display(active_model, models), session.mode)
        if session.has_pending_checkpoint:
            _console(color=color).print(
                "[bold #f59e0b]⚡ 检测到上次未完成的挂起轮次（checkpoint.json）。输入 /resume 继续处理，或 /cancel 放弃该轮次。[/]"
            )

    try:
        while True:
            line = terminal_input.read() if terminal_input is not None else input()
            if terminal:
                # Move cursor up and clear the raw slash command line when it's a slash utility so chat stays clean
                stripped_line = line.strip()
                if stripped_line.startswith("/") and stripped_line.split()[0] in {
                    "/memory", "/tools", "/tool", "/skill", "/mcp", "/model", "/mode", "/expand", "/hitl", "/context", "/task",
                }:
                    # Erase the 2-line input frame (`╭──...──╮` + `│ › /command`)
                    sys.stdout.write("\x1b[1A\x1b[2K\x1b[1A\x1b[2K\r")
                    sys.stdout.flush()

            command = line.split(maxsplit=1)
            if not command:
                continue

            if command[0] == "/expand":
                _show_tool_inspector(tool_display, color=color)
                continue

            if command[0] == "/model":
                alias = command[1].strip() if len(command) == 2 else choose_model(
                    models,
                    active_model,
                    read_input=read_fn,
                )
                if not alias or alias == active_model:
                    continue
                try:
                    select_model(alias)
                except (ValueError, RuntimeError) as error:
                    print(f"切换失败。{error}", file=sys.stderr)
                else:
                    active_model = alias
                    if terminal_input is not None:
                        terminal_input.active_model = active_model
                        terminal_input.transient_notice = f"已切换到 {_format_model_display(alias, models)}"
                    else:
                        label = models.get(alias, alias)
                        print(f"已切换到 {label} [{alias}]。", flush=True)
                continue

            if command[0] in {"/mode", "/plan", "/goal"}:
                if command[0] == "/plan":
                    target_mode = "plan"
                    extra_prompt = command[1].strip() if len(command) == 2 else ""
                elif command[0] == "/goal":
                    target_mode = "goal"
                    extra_prompt = command[1].strip() if len(command) == 2 else ""
                else:
                    target_mode = command[1].strip().casefold() if len(command) == 2 else ""
                    extra_prompt = ""
                    if not target_mode and _use_tui_menu(read_fn):
                        mode_items = [
                            MenuItem("normal", "Normal 标准执行模式（读写工具 + Default/Auto 审批）", tag="NORMAL"),
                            MenuItem("plan", "Plan 只读规划模式（只读探测 + 生成结构化计划）", tag="PLAN"),
                            MenuItem("goal", "Goal 自主长任务模式（围绕计划步骤与验证命令自动多轮推进）", tag="GOAL"),
                        ]
                        cur_idx = (
                            ["normal", "plan", "goal"].index(session.mode)
                            if session.mode in {"normal", "plan", "goal"}
                            else 0
                        )
                        outcome = run_interactive_menu(
                            title="切换运行模式 (/mode)",
                            subtitle=f"当前模式: {session.mode.upper()} · 退出自动擦除",
                            items_provider=lambda: mode_items,
                            footer_hints=(("↑/↓/鼠标", "选择"), ("Enter", "切换模式"), ("Esc/q", "返回")),
                            initial_index=cur_idx,
                            erase_when_done=True,
                        )
                        if outcome and outcome.item:
                            target_mode = outcome.item.key
                if target_mode not in {"normal", "plan", "goal"}:
                    if not terminal:
                        print(f"当前运行模式：{session.mode}。用法：/mode normal|plan|goal（或 /plan [任务]、/goal [目标]）")
                    continue
                session.set_mode(target_mode)
                if terminal_input is not None:
                    terminal_input.transient_notice = f"模式已切换为 {target_mode.upper()}"
                else:
                    print(f"已切换运行模式为：{target_mode}。")
                if extra_prompt:
                    if target_mode == "goal":
                        identity = project_identity(Path.cwd())
                        TaskStateStore(state_root() / "projects" / identity / "task.json").set_goal(extra_prompt)
                    try:
                        show_result(
                            session,
                            session.send(extra_prompt),
                            permission_mode=permission_mode,
                            read_input=read_fn,
                        )
                    except (ValueError, RuntimeError, OSError) as error:
                        print(f"本轮失败。{error}", file=sys.stderr)
                continue

            if command[0] == "/resume":
                if not session.has_pending_checkpoint or session.pending_pause is None:
                    print("当前没有挂起的断点轮次。")
                    continue
                try:
                    show_result(
                        session,
                        session.pending_pause,
                        permission_mode=permission_mode,
                        read_input=read_fn,
                    )
                except (ValueError, RuntimeError, OSError) as error:
                    print(f"恢复失败：{error}", file=sys.stderr)
                continue

            if command[0] == "/memory":
                _manage_memory(
                    line,
                    memory or MemoryManager(state_root(), project_identity(Path.cwd())),
                    read_input=read_fn,
                )
                continue

            if command[0] in {"/tools", "/tool"}:
                _manage_tools(line, tools, read_input=read_fn)
                continue

            if command[0] == "/skill":
                prompt = _manage_skills(line, skill_registry, read_input=read_fn)
                if prompt:
                    try:
                        show_result(
                            session,
                            session.send(prompt),
                            permission_mode=permission_mode,
                            read_input=read_fn,
                        )
                    except (ValueError, RuntimeError, OSError) as error:
                        print(f"Skill 执行失败：{error}", file=sys.stderr)
                continue

            if command[0] == "/mcp":
                _manage_mcp(line, mcp_manager, tools, read_input=read_fn)
                continue

            if command[0] == "/hitl":
                action = command[1].strip().casefold() if len(command) == 2 else ""
                aliases = {"default": "default", "on": "default", "auto": "auto", "off": "auto"}
                if action not in aliases:
                    print("用法：/hitl default|auto")
                elif not terminal and aliases[action] == "auto":
                    print("非交互模式不会自动批准工具操作。")
                else:
                    permission_mode.set(aliases[action])
                    if terminal_input is not None:
                        terminal_input.transient_notice = f"权限模式已切换为 {permission_mode.mode.upper()}"
                    else:
                        label = (
                            "Auto（自动批准沙箱内工具；沙箱外提权仍需审批）"
                            if permission_mode.auto_approve
                            else "Default（每项副作用操作单独审批）"
                        )
                        print(f"当前权限模式：{label}")
                continue

            if command[0] == "/task":
                _manage_task(line, Path.cwd(), read_input=read_fn)
                continue

            if command[0] == "/context":
                _show_context_trace(read_input=read_fn)
                continue

            if command[0] == "/new":
                try:
                    session.new_chat()
                    tool_display.entries.clear()
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
                show_result(
                    session,
                    reply,
                    permission_mode=permission_mode,
                    read_input=read_fn,
                )
            except (ValueError, RuntimeError, OSError) as error:
                if color:
                    Console(file=sys.stderr).print(
                        Panel(
                            Text(str(error)),
                            title="[bold #ef4444]本轮失败[/]",
                            border_style="#ef4444",
                            box=box.ROUNDED,
                        )
                    )
                else:
                    print(f"本轮失败。{error}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        session.cancel()
        if terminal:
            print()


# ---------------------------------------------------------------------------
# 1. Human-friendly /memory (TUI arrow/mouse selector with erase_when_done)
# ---------------------------------------------------------------------------


def _collect_numbered_memories(manager: MemoryManager, scope_filter: str | None = None) -> list[Memory]:
    scopes = (scope_filter,) if scope_filter in {"global", "project"} else ("global", "project")
    combined: list[Memory] = []
    for scope in scopes:
        combined.extend(manager.list(scope))
    return combined


def _print_numbered_memories(items: list[Memory]) -> None:
    if not items:
        print("当前没有任何记忆。（Agent 会在对话中自动记录长期偏好与项目事实）")
        return
    print("当前记忆列表：")
    for idx, item in enumerate(items, start=1):
        scope_tag = "全局" if item.scope == "global" else "项目"
        conflict_tag = " [冲突未决]" if item.status == "conflict" else ""
        rev_tag = f" (历史修改 {len(item.history or [])} 次)" if item.history else ""
        print(f"  {idx}. [{scope_tag}/{item.category}]{conflict_tag} {item.content}{rev_tag}")


def _resolve_memory_selection(
    token: str,
    items: list[Memory],
    manager: MemoryManager,
    default_scope: str = "project",
) -> tuple[str, str]:
    if token.isdigit():
        idx = int(token)
        if not 1 <= idx <= len(items):
            raise ValueError(f"编号超出范围（请输入 1 到 {len(items)}）。")
        chosen = items[idx - 1]
        return chosen.scope, chosen.id
    for scope in (default_scope, "global" if default_scope == "project" else "project"):
        for item in manager.list(scope, include_history=True):
            if item.id.startswith(token):
                return scope, item.id
    return default_scope, token


def _run_memory_tui(manager: MemoryManager, scope_filter: str | None) -> None:
    def build_items() -> list[MenuItem]:
        raw = _collect_numbered_memories(manager, scope_filter)
        menu_items: list[MenuItem] = []
        for m in raw:
            scope_tag = "全局" if m.scope == "global" else "项目"
            tag = f"{scope_tag}/{m.category}" + (" · 冲突" if m.status == "conflict" else "")
            meta = f"(已改 {len(m.history)} 次)" if m.history else ""
            menu_items.append(MenuItem(key=m.id, label=m.content, tag=tag, meta=meta, payload=m))
        return menu_items

    def on_action(action: str, item: MenuItem | None, idx: int) -> str | MenuOutcome | None:
        if action == "s":
            return MenuOutcome("s", idx, item)
        if item is None:
            return None
        mem: Memory = item.payload
        if action == "d":
            manager.delete(mem.scope, mem.id)
            return f"已删除记忆：{mem.content[:30]}"
        if action == "r":
            if mem.status != "conflict":
                return "该条记忆不属于未解决的冲突。"
            resolved = manager.resolve(mem.scope, mem.id)
            return f"已采纳：{resolved.content[:30]}（同组冲突已归档）"
        if action == "h":
            full = manager.history(mem.scope, mem.id)
            lines = [f"当前内容：{full.content}", f"范围/类别：{full.scope}/{full.category} · 更新时间：{full.updated_at}"]
            if not full.history:
                lines.append("（暂无历史修改版本）")
            else:
                lines.append("历史版本：")
                for rev in full.history:
                    lines.append(f"  - [{rev.get('source_type')}] {rev.get('updated_at')}: {rev.get('content', '')}")
            return "__DETAIL__:" + "\n".join(lines)
        if action in {"enter", "e"}:
            return MenuOutcome("e", idx, item)
        return "按 Enter/e 修改 · d 删除 · h 查看历史 · s 搜索 · Esc 退出"

    cursor = 0
    while True:
        outcome = run_interactive_menu(
            title="长期记忆管理 (/memory)",
            subtitle="汇总全局与当前项目记忆 · 退出后自动擦除不污染聊天",
            items_provider=build_items,
            footer_hints=(
                ("↑/↓/鼠标", "选择"),
                ("Enter/e", "修改内容"),
                ("d/Del", "删除"),
                ("h", "历史版本"),
                ("r", "解决冲突"),
                ("s", "搜索"),
                ("Esc/q", "退出"),
            ),
            on_key_action=on_action,
            initial_index=cursor,
            erase_when_done=True,
        )
        if outcome is None:
            break
        cursor = outcome.index
        if outcome.action == "s":
            query = transient_input("输入搜索关键词（回车返回列表）")
            if query:
                hits = manager.search(query, scope="current", limit=10)
                if not hits:
                    detail = f"未找到与 {query!r} 匹配的记忆。"
                else:
                    detail = "\n".join(
                        f"{i}. [{'全局' if h.memory.scope == 'global' else '项目'}/{h.memory.category}] "
                        f"{h.memory.content} (匹配度={h.score:.3f})"
                        for i, h in enumerate(hits, 1)
                    )
                run_interactive_menu(
                    title=f"记忆搜索结果：{query}",
                    subtitle="按 Esc 或 Enter 返回记忆列表",
                    items_provider=lambda: [MenuItem("back", line) for line in detail.splitlines()],
                    footer_hints=(("Enter/Esc", "返回列表"),),
                    erase_when_done=True,
                )
            continue
        if outcome.item is None:
            break
        mem: Memory = outcome.item.payload
        if outcome.action == "e":
            new_text = transient_input("修改记忆内容（回车保存，清空/Ctrl+C 取消）", default=mem.content)
            if new_text and new_text != mem.content:
                try:
                    manager.update(mem.scope, mem.id, new_text)
                except (ValueError, KeyError, RuntimeError):
                    pass


def _manage_memory(
    line: str,
    manager: MemoryManager,
    *,
    read_input: Callable[[str], str] | None = None,
) -> None:
    tail = line[len("/memory") :].strip()

    if tail in {"", "global", "project"}:
        scope_filter = tail if tail in {"global", "project"} else None
        if _use_tui_menu(read_input):
            _run_memory_tui(manager, scope_filter)
            return
        items = _collect_numbered_memories(manager, scope_filter)
        _print_numbered_memories(items)
        if not items:
            return
        print("\n操作提示：输入编号（如 1）选择操作 · e <编号> 修改 · d <编号> 删除 · s <关键词> 搜索 · 回车退出")
        choice = _ask_prompt("选择记忆编号或操作", read_input)
        if not choice or choice.casefold() in {"q", "quit", "exit"}:
            return
        _execute_memory_menu_choice(choice, items, manager, read_input=read_input)
        return

    parts = tail.split()
    if parts[0] in {"help", "?"}:
        print(
            "用法：\n"
            "  /memory                  打开交互式记忆菜单（支持 ↑/↓ 方向键、鼠标点选、Enter 修改、d 删除，退出自动擦除）\n"
            "  /memory e <编号> <新内容> 直接修改指定编号的记忆\n"
            "  /memory d <编号>         直接删除指定编号的记忆\n"
            "  /memory s <关键词>       搜索记忆"
        )
        return

    items = _collect_numbered_memories(manager)
    if parts[0].isdigit() or parts[0].casefold() in {"e", "d", "h", "r", "s"}:
        _execute_memory_menu_choice(tail, items, manager, read_input=read_input)
        return

    if parts[0] in {"global", "project"}:
        scope = parts[0]
        action_line = tail[len(scope) :].strip()
        scoped_items = _collect_numbered_memories(manager, scope)
    else:
        scope = "project"
        action_line = tail
        scoped_items = items

    args = action_line.split(maxsplit=2)
    action = args[0].casefold() if args else "list"
    try:
        if action == "list":
            _print_numbered_memories(scoped_items)
        elif action == "search":
            query = action_line[len(args[0]) :].strip()
            search_scope = scope if parts[0] in {"global", "project"} else "current"
            hits = manager.search(query, scope=search_scope, limit=10)
            for idx, hit in enumerate(hits, start=1):
                scope_tag = "全局" if hit.memory.scope == "global" else "项目"
                print(
                    f"  {idx}. [{scope_tag}/{hit.memory.category}] {hit.memory.content} "
                    f"(匹配度={hit.score:.3f}；依据={', '.join(hit.reasons)})"
                )
            if not hits:
                print("没有匹配的记忆。")
        elif action == "edit":
            if len(args) < 3:
                raise ValueError("用法：/memory edit <编号> <新内容>")
            target_scope, target_id = _resolve_memory_selection(args[1], scoped_items, manager, scope)
            item = manager.update(target_scope, target_id, args[2])
            print(f"已更新记忆：{item.content}")
        elif action == "history":
            if len(args) < 2:
                raise ValueError("用法：/memory history <编号>")
            target_scope, target_id = _resolve_memory_selection(args[1], scoped_items, manager, scope)
            item = manager.history(target_scope, target_id)
            _print_memory(item)
            for revision in item.history or []:
                print(
                    f"  历史 [{revision.get('source_type')}] {revision.get('updated_at')} {revision.get('content', '')}"
                )
        elif action == "delete":
            if len(args) < 2:
                raise ValueError("用法：/memory delete <编号>")
            target_scope, target_id = _resolve_memory_selection(args[1], scoped_items, manager, scope)
            manager.delete(target_scope, target_id)
            print("已删除记忆；它不会再进入 Context。")
        elif action == "conflicts":
            conflicts = [m for m in scoped_items if m.status == "conflict"]
            if not conflicts:
                print("没有待处理的冲突。")
            else:
                _print_numbered_memories(conflicts)
        elif action == "resolve":
            if len(args) < 2:
                raise ValueError("用法：/memory resolve <保留的编号>")
            target_scope, target_id = _resolve_memory_selection(args[1], scoped_items, manager, scope)
            item = manager.resolve(target_scope, target_id)
            print(f"已保留：{item.content}；同组其他候选已归档。")
        else:
            raise ValueError("不支持的操作。直接输入 /memory 打开交互式菜单。")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"记忆操作失败：{error}", file=sys.stderr)


def _execute_memory_menu_choice(
    choice: str,
    items: list[Memory],
    manager: MemoryManager,
    *,
    read_input: Callable[[str], str] | None = None,
) -> None:
    tokens = choice.strip().split(maxsplit=2)
    if not tokens:
        return
    try:
        head = tokens[0].casefold()
        if head in {"s", "search"}:
            query = choice.strip()[len(tokens[0]) :].strip()
            if not query:
                query = _ask_prompt("输入搜索关键词", read_input)
            if not query:
                return
            hits = manager.search(query, scope="current", limit=10)
            if not hits:
                print("没有匹配的记忆。")
                return
            for idx, hit in enumerate(hits, start=1):
                scope_tag = "全局" if hit.memory.scope == "global" else "项目"
                print(f"  {idx}. [{scope_tag}/{hit.memory.category}] {hit.memory.content} (匹配度={hit.score:.3f})")
            return

        if head.isdigit():
            idx = int(head)
            if not 1 <= idx <= len(items):
                raise ValueError(f"编号超出范围（请输入 1 到 {len(items)}）。")
            target = items[idx - 1]
            print(f"已选第 {idx} 条：[{target.scope}/{target.category}] {target.content}")
            action = (
                tokens[1].casefold()
                if len(tokens) > 1
                else _ask_prompt("选择操作：e 修改 · d 删除 · h 查看历史 · r 解决冲突 · 回车取消", read_input).casefold()
            )
            if not action:
                return
            if action in {"e", "edit"}:
                new_text = tokens[2] if len(tokens) > 2 else _ask_prompt("输入修改后的新内容（回车取消）", read_input)
                if not new_text:
                    print("已取消修改。")
                    return
                updated = manager.update(target.scope, target.id, new_text)
                print(f"已更新第 {idx} 条记忆：{updated.content}")
            elif action in {"d", "del", "delete"}:
                manager.delete(target.scope, target.id)
                print(f"已删除第 {idx} 条记忆；它不会再进入 Context。")
            elif action in {"h", "history"}:
                full = manager.history(target.scope, target.id)
                _print_memory(full)
                if not full.history:
                    print("  （暂无历史修改版本）")
                for revision in full.history or []:
                    print(
                        f"  历史 [{revision.get('source_type')}] {revision.get('updated_at')} {revision.get('content', '')}"
                    )
            elif action in {"r", "resolve"}:
                resolved = manager.resolve(target.scope, target.id)
                print(f"已采纳第 {idx} 条记忆：{resolved.content}；同冲突组其他候选已归档。")
            else:
                print("未知操作，请输入 e（修改）、d（删除）、h（历史）或 r（解决冲突）。")
            return

        if head in {"e", "edit"}:
            if len(tokens) < 2:
                raise ValueError("请指定记忆编号，例如：e 1")
            scope, mid = _resolve_memory_selection(tokens[1], items, manager)
            new_text = tokens[2] if len(tokens) > 2 else _ask_prompt("输入修改后的新内容（回车取消）", read_input)
            if not new_text:
                print("已取消修改。")
                return
            updated = manager.update(scope, mid, new_text)
            print(f"已更新记忆：{updated.content}")
            return

        if head in {"d", "del", "delete"}:
            if len(tokens) < 2:
                raise ValueError("请指定记忆编号，例如：d 1")
            scope, mid = _resolve_memory_selection(tokens[1], items, manager)
            manager.delete(scope, mid)
            print("已删除记忆；它不会再进入 Context。")
            return

        if head in {"h", "history"}:
            if len(tokens) < 2:
                raise ValueError("请指定记忆编号，例如：h 1")
            scope, mid = _resolve_memory_selection(tokens[1], items, manager)
            full = manager.history(scope, mid)
            _print_memory(full)
            for revision in full.history or []:
                print(
                    f"  历史 [{revision.get('source_type')}] {revision.get('updated_at')} {revision.get('content', '')}"
                )
            return

        if head in {"r", "resolve"}:
            if len(tokens) < 2:
                raise ValueError("请指定记忆编号，例如：r 1")
            scope, mid = _resolve_memory_selection(tokens[1], items, manager)
            resolved = manager.resolve(scope, mid)
            print(f"已保留：{resolved.content}；同组其他候选已归档。")
            return

        raise ValueError("请输入有效的编号（如 1）或操作（e 1 / d 1 / s 关键词）。")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"记忆操作失败：{error}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 2. Human-friendly /tools (TUI arrow/mouse live toggle with erase_when_done)
# ---------------------------------------------------------------------------


def _run_tools_tui(registry: ToolRegistry) -> None:
    def build_items() -> list[MenuItem]:
        items: list[MenuItem] = []
        for tool, enabled in registry.all_registered():
            mode_tag = "只读" if getattr(tool, "read_only", False) else ("需审批" if tool.requires_approval else "读写")
            items.append(
                MenuItem(
                    key=tool.name,
                    label=f"{tool.name:<18}",
                    enabled=enabled,
                    tag=mode_tag,
                    meta=tool.description[:60],
                )
            )
        return items

    def on_action(action: str, item: MenuItem | None, _idx: int) -> str | MenuOutcome | None:
        if action == "a":
            all_reg = registry.all_registered()
            any_disabled = any(not en for _, en in all_reg)
            for t, _ in all_reg:
                registry.set_enabled(t.name, any_disabled)
            return f"已{'全部启用' if any_disabled else '全部停用'}所有工具（已持久化到 ~/.minicc/tools.json）"
        if item is None:
            return None
        if action in {"enter", "space", "t"}:
            new_state = registry.toggle(item.key)
            return f"已{'启用' if new_state else '停用'}工具：{item.key}（已保存）"
        return "按 Space/Enter 切换启用/停用 · a 全选/全关 · Esc 退出"

    run_interactive_menu(
        title="工具开关管理 (/tools)",
        subtitle="支持 ↑/↓ 方向键与鼠标点击切换 · 退出后自动擦除不污染聊天",
        items_provider=build_items,
        footer_hints=(
            ("↑/↓/鼠标", "移动"),
            ("Space/Enter", "切换启用/停用"),
            ("a", "全开/全关"),
            ("Esc/q", "完成并关闭"),
        ),
        on_key_action=on_action,
        erase_when_done=True,
    )


def _manage_tools(
    line: str,
    registry: ToolRegistry | None,
    *,
    read_input: Callable[[str], str] | None = None,
) -> None:
    if registry is None:
        print("当前会话未启用工具注册表（--chat-only）。", file=sys.stderr)
        return
    entries = list(registry.all_registered())
    if not entries:
        print("当前没有已注册的工具。")
        return

    tail = line.split(maxsplit=1)[1].strip() if len(line.split(maxsplit=1)) == 2 else ""
    if not tail:
        if _use_tui_menu(read_input):
            _run_tools_tui(registry)
            return
        _print_numbered_tools(entries)
        print("\n操作提示：输入编号（如 1 或 2 5）切换启用/停用 · on <编号> 启用 · off <编号> 停用 · 回车退出")
        choice = _ask_prompt("选择要切换的工具编号", read_input)
        if not choice or choice.casefold() in {"q", "quit", "exit"}:
            return
        _apply_tool_choice(choice, entries, registry)
        return

    if tail in {"help", "?"}:
        print("用法：/tools 打开交互式工具开关面板（支持 ↑/↓ 与鼠标点击切换，退出自动擦除）；或 /tools enable|disable <编号|名称>")
        return
    if tail == "list":
        _print_numbered_tools(entries)
        return
    _apply_tool_choice(tail, entries, registry)


def _print_numbered_tools(entries: list[tuple]) -> None:
    print("已注册工具列表：")
    for idx, (tool, enabled) in enumerate(entries, start=1):
        status = "✓ 启用" if enabled else "✗ 停用"
        mode_tag = "只读" if getattr(tool, "read_only", False) else ("需审批" if tool.requires_approval else "读写")
        print(f"  {idx:>2}. [{status}] {tool.name:<20} ({mode_tag}) {tool.description}")


def _resolve_tool_token(token: str, entries: list[tuple]) -> str:
    if token.isdigit():
        idx = int(token)
        if not 1 <= idx <= len(entries):
            raise ValueError(f"工具编号超出范围（请输入 1 到 {len(entries)}）。")
        return entries[idx - 1][0].name
    for tool, _ in entries:
        if tool.name == token:
            return tool.name
    raise ValueError(f"未找到工具：{token}")


def _apply_tool_choice(choice: str, entries: list[tuple], registry: ToolRegistry) -> None:
    tokens = choice.strip().split()
    if not tokens:
        return
    head = tokens[0].casefold()
    try:
        if head in {"on", "enable", "off", "disable"}:
            if len(tokens) < 2:
                raise ValueError(f"请指定工具编号或名称，例如：{head} 1")
            target_state = head in {"on", "enable"}
            for raw in tokens[1:]:
                if raw.casefold() == "all":
                    for tool, _ in entries:
                        registry.set_enabled(tool.name, target_state)
                    print(f"已{'启用' if target_state else '停用'}全部工具。")
                    return
                name = _resolve_tool_token(raw, entries)
                registry.set_enabled(name, target_state)
                print(f"已{'启用' if target_state else '停用'}工具：{name}。")
            return

        targets = tokens[1:] if head in {"t", "toggle"} else tokens
        for raw in targets:
            name = _resolve_tool_token(raw, entries)
            new_state = registry.toggle(name)
            print(f"已{'启用' if new_state else '停用'}工具：{name}。")
    except ValueError as error:
        print(f"工具操作失败：{error}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 3. Human-friendly /skill (TUI arrow/mouse menu with erase_when_done)
# ---------------------------------------------------------------------------


def _run_skills_tui(registry: SkillRegistry) -> str | None:
    def build_items() -> list[MenuItem]:
        items: list[MenuItem] = []
        for s in registry.all_skills():
            tags = ",".join(s.tags[:3]) if s.tags else s.source
            items.append(
                MenuItem(
                    key=s.name,
                    label=s.name,
                    enabled=s.enabled,
                    tag=tags,
                    meta=s.description[:60],
                    payload=s,
                )
            )
        return items

    def on_action(action: str, item: MenuItem | None, idx: int) -> str | MenuOutcome | None:
        if action == "0":
            new_global = not registry.enabled
            if registry.set_enabled(new_global):
                return f"已{'开启' if new_global else '关闭'} Skills 全局总开关"
            return "配置中 skills.enabled=false，无法在运行时开启。"
        if item is None:
            return None
        skill = item.payload
        if action in {"enter", "space", "t"}:
            if skill.enabled:
                registry.disable(skill.name)
                return f"已停用 Skill：{skill.name}"
            registry.enable(skill.name)
            return f"已启用 Skill：{skill.name}"
        if action == "v":
            loaded = registry.load(skill.name, user_initiated=True)
            body = loaded.content if loaded and loaded.content else "（无法读取或已停用）"
            return f"__DETAIL__:【Skill: {skill.name}】\n{body}"
        if action == "u":
            return MenuOutcome("u", idx, item)
        return "按 Space/Enter 切换启用/停用 · v 查看正文 · u 调用执行 · 0 总开关 · Esc 退出"

    outcome = run_interactive_menu(
        title=f"Skill 技能管理 (/skill) · 总开关: {'● 开启' if registry.enabled else '○ 关闭'}",
        subtitle="支持 ↑/↓ 方向键与鼠标点击 · 退出自动擦除",
        items_provider=build_items,
        footer_hints=(
            ("↑/↓/鼠标", "选择"),
            ("Space/Enter", "切换启用/停用"),
            ("v", "查看正文"),
            ("u", "调用该 Skill"),
            ("0", "总开关"),
            ("Esc/q", "退出"),
        ),
        on_key_action=on_action,
        erase_when_done=True,
    )
    if outcome is None or outcome.item is None:
        return None
    if outcome.action == "u":
        skill_name = outcome.item.key
        task = transient_input(f"输入用 {skill_name} 执行的任务描述")
        return _build_skill_use_prompt(registry, skill_name, task)
    return None


def _manage_skills(
    line: str,
    registry: SkillRegistry | None,
    *,
    read_input: Callable[[str], str] | None = None,
) -> str | None:
    if registry is None:
        print("Skill 管理器未配置。", file=sys.stderr)
        return None
    parts = line.split(maxsplit=3)
    skills = registry.all_skills()

    if len(parts) == 1:
        if _use_tui_menu(read_input):
            return _run_skills_tui(registry)
        _print_numbered_skills(registry, skills)
        print(
            "\n操作提示：输入编号（如 1）切换启用/停用或操作 · t <编号> 切换 · v <编号> 查看正文 · "
            "u <编号> [任务] 调用 · 0 切换总开关 · 回车退出"
        )
        choice = _ask_prompt("选择 Skill 编号或操作", read_input)
        if not choice or choice.casefold() in {"q", "quit", "exit"}:
            return None
        return _execute_skill_menu_choice(choice, registry, skills, read_input=read_input)

    action = parts[1].casefold()
    if action in {"help", "?"}:
        print(
            "用法：\n"
            "  /skill                   打开交互式 Skill 菜单（支持 ↑/↓ 与鼠标点击启停/查看/调用，退出自动擦除）\n"
            "  /skill on|off            开启或关闭 Skills 总开关\n"
            "  /skill enable|disable <编号|名称>\n"
            "  /skill show <编号|名称>   查看 Skill 完整指令正文\n"
            "  /skill use <编号|名称> [任务]  使用指定 Skill 执行任务\n"
            "  /skill reload            重新扫描本地 Skill 目录"
        )
        return None
    if action == "list":
        _print_numbered_skills(registry, skills)
        return None
    if action == "on":
        print(
            "已启用 Skills。"
            if registry.set_enabled(True)
            else "配置中的 skills.enabled=false，无法在运行时开启。"
        )
        return None
    if action == "off":
        registry.set_enabled(False)
        print("已关闭所有 Skills。")
        return None
    if action == "reload":
        registry.reload()
        print(f"已重新扫描 Skills：{len(registry.all_skills())} 个。")
        return None
    if action.isdigit() or action in {"t", "v", "u"}:
        return _execute_skill_menu_choice(line[len("/skill") :].strip(), registry, skills, read_input=read_input)
    if action in {"enable", "disable"}:
        if len(parts) < 3:
            print(f"用法：/skill {action} <编号|名称>")
            return None
        name = _resolve_skill_name(parts[2], skills)
        changed = registry.enable(name) if action == "enable" else registry.disable(name)
        print(
            f"已{'启用' if action == 'enable' else '停用'} Skill {name}。"
            if changed
            else f"未找到 Skill：{parts[2]}"
        )
        return None
    if action == "show":
        if len(parts) < 3:
            print("用法：/skill show <编号|名称>")
            return None
        name = _resolve_skill_name(parts[2], skills)
        skill = registry.load(name, user_initiated=True)
        if skill is None:
            print(f"Skill 不存在、已停用或不可供用户调用：{name}")
        else:
            print(skill.content or "（空 Skill）")
        return None
    if action == "use":
        if len(parts) < 3:
            print("用法：/skill use <编号|名称> [任务]")
            return None
        name = _resolve_skill_name(parts[2], skills)
        task = parts[3] if len(parts) > 3 else ""
        return _build_skill_use_prompt(registry, name, task)

    _print_numbered_skills(registry, skills)
    return None


def _print_numbered_skills(registry: SkillRegistry, skills: list) -> None:
    print(f"Skills 总开关：{'✓ 启用' if registry.enabled else '✗ 关闭'}（输入 0 可切换总开关）")
    if not skills:
        print("没有发现已安装的 Skill。")
        return
    for idx, skill in enumerate(skills, start=1):
        state = "✓ 启用" if skill.enabled else "✗ 停用"
        tags = f" [标签：{', '.join(skill.tags)}]" if skill.tags else ""
        print(f"  {idx}. [{state}] {skill.name} ({skill.source}){tags}：{skill.description}")


def _resolve_skill_name(token: str, skills: list) -> str:
    if token.isdigit():
        idx = int(token)
        if 1 <= idx <= len(skills):
            return skills[idx - 1].name
    return token


def _build_skill_use_prompt(registry: SkillRegistry, name: str, task: str) -> str | None:
    skill = registry.load(name, user_initiated=True)
    if skill is None or skill.content is None:
        print(f"Skill 不存在、已停用或不可供用户调用：{name}")
        return None
    actual_task = task.strip() or "请按以下 Skill 指令处理当前任务。"
    return (
        f"用户通过 /skill 明确调用 {skill.name}。以下是已加载的 Skill 正文，请按其流程完成任务；"
        "不要再次调用 load_skill 加载同一个 Skill。\n\n"
        f'<skill_content name="{skill.name}">\n{skill.content[:12_000]}\n</skill_content>\n\n'
        f"用户任务：{actual_task}"
    )


def _execute_skill_menu_choice(
    choice: str,
    registry: SkillRegistry,
    skills: list,
    *,
    read_input: Callable[[str], str] | None = None,
) -> str | None:
    tokens = choice.strip().split(maxsplit=2)
    if not tokens:
        return None
    head = tokens[0].casefold()
    if head == "0":
        new_global = not registry.enabled
        if registry.set_enabled(new_global):
            print(f"已{'启用' if new_global else '关闭'} Skills 总开关。")
        else:
            print("配置中的 skills.enabled=false，无法在运行时开启。")
        return None

    if head.isdigit():
        idx = int(head)
        if not 1 <= idx <= len(skills):
            print(f"编号超出范围（请输入 1 到 {len(skills)}）。", file=sys.stderr)
            return None
        target = skills[idx - 1]
        sub = (
            tokens[1].casefold()
            if len(tokens) > 1
            else _ask_prompt(
                f"已选 {target.name}：t 切换启用/停用（默认） · v 查看正文 · u 执行任务", read_input
            ).casefold()
        )
        if sub in {"", "t", "toggle"}:
            if target.enabled:
                registry.disable(target.name)
                print(f"已停用 Skill：{target.name}。")
            else:
                registry.enable(target.name)
                print(f"已启用 Skill：{target.name}。")
            return None
        if sub in {"v", "view", "show"}:
            loaded = registry.load(target.name, user_initiated=True)
            print(loaded.content if loaded and loaded.content else "（无法读取或已停用）")
            return None
        if sub in {"u", "use"}:
            task = tokens[2] if len(tokens) > 2 else _ask_prompt("输入要执行的任务描述", read_input)
            return _build_skill_use_prompt(registry, target.name, task)
        return None

    if head in {"t", "toggle"} and len(tokens) >= 2:
        name = _resolve_skill_name(tokens[1], skills)
        target = next((s for s in skills if s.name == name), None)
        if target is None:
            print(f"未找到 Skill：{tokens[1]}", file=sys.stderr)
            return None
        if target.enabled:
            registry.disable(target.name)
            print(f"已停用 Skill：{target.name}。")
        else:
            registry.enable(target.name)
            print(f"已启用 Skill：{target.name}。")
        return None

    if head in {"v", "view", "show"} and len(tokens) >= 2:
        name = _resolve_skill_name(tokens[1], skills)
        loaded = registry.load(name, user_initiated=True)
        print(loaded.content if loaded and loaded.content else f"Skill 不存在或已停用：{name}")
        return None

    if head in {"u", "use"} and len(tokens) >= 2:
        name = _resolve_skill_name(tokens[1], skills)
        task = tokens[2] if len(tokens) > 2 else _ask_prompt("输入要执行的任务描述", read_input)
        return _build_skill_use_prompt(registry, name, task)

    print("请输入有效的 Skill 编号或操作（t 1 / v 1 / u 1）。", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# 4. Human-friendly /mcp (TUI arrow/mouse menu with erase_when_done)
# ---------------------------------------------------------------------------


def _run_mcp_tui(mcp_manager: McpManager) -> None:
    def build_items() -> list[MenuItem]:
        items: list[MenuItem] = []
        for srv in mcp_manager.server_statuses():
            tools = srv.get("tools", [])
            tool_names = ", ".join(t["remote_name"] for t in tools) if tools else "无已加载工具"
            ro = "只读" if srv["read_only"] else "读写"
            items.append(
                MenuItem(
                    key=srv["name"],
                    label=f"{srv['name']:<16}",
                    enabled=srv["enabled"],
                    tag=ro,
                    meta=f"工具: {tool_names} ({srv['command']})",
                )
            )
        return items

    def on_action(action: str, item: MenuItem | None, _idx: int) -> str | MenuOutcome | None:
        if item is None:
            return None
        if action in {"enter", "space", "t"}:
            new_state = mcp_manager.toggle_server(item.key)
            return f"已{'启用' if new_state else '停用'} MCP Server：{item.key}（已保存到 ~/.minicc/mcp.json）"
        return "按 Space/Enter 切换开启/关闭 · Esc 退出"

    run_interactive_menu(
        title="MCP Server 管理 (/mcp)",
        subtitle="支持 ↑/↓ 方向键与鼠标点击切换 · 退出后自动擦除不污染聊天",
        items_provider=build_items,
        footer_hints=(
            ("↑/↓/鼠标", "移动"),
            ("Space/Enter", "切换开启/关闭"),
            ("Esc/q", "完成并关闭"),
        ),
        on_key_action=on_action,
        erase_when_done=True,
    )


def _manage_mcp(
    line: str,
    mcp_manager: McpManager | None,
    tool_registry: ToolRegistry | None = None,
    *,
    read_input: Callable[[str], str] | None = None,
) -> None:
    if mcp_manager is None or not mcp_manager.servers:
        if _use_tui_menu(read_input):
            run_interactive_menu(
                title="MCP Server 管理 (/mcp)",
                subtitle="当前未配置任何 MCP Server（可在 models.json 的 mcp_servers 中添加配置）",
                items_provider=lambda: [],
                footer_hints=(("Esc/q", "关闭"),),
                erase_when_done=True,
            )
        else:
            print("当前未配置任何 MCP Server（可在 models.json 的 mcp_servers 中添加配置）。")
        return
    statuses = mcp_manager.server_statuses()
    parts = line.split(maxsplit=2)

    if len(parts) == 1:
        if _use_tui_menu(read_input):
            _run_mcp_tui(mcp_manager)
            return
        _print_numbered_mcp(statuses)
        print("\n操作提示：输入 Server 编号（如 1）切换开启/关闭 · on <编号> 开启 · off <编号> 关闭 · 回车退出")
        choice = _ask_prompt("选择要切换的 MCP Server 编号", read_input)
        if not choice or choice.casefold() in {"q", "quit", "exit"}:
            return
        _apply_mcp_choice(choice, statuses, mcp_manager, tool_registry)
        return

    action = parts[1].casefold()
    if action in {"help", "?"}:
        print("用法：/mcp 打开交互式 MCP 管理面板（支持 ↑/↓ 与鼠标点击切换，退出自动擦除）；或 /mcp enable|disable <编号|名称>")
        return
    if action == "list":
        _print_numbered_mcp(statuses)
        return
    _apply_mcp_choice(line[len("/mcp") :].strip(), statuses, mcp_manager, tool_registry)


def _print_numbered_mcp(statuses: list[dict]) -> None:
    print("已配置 MCP Servers：")
    for idx, srv in enumerate(statuses, start=1):
        state = "✓ 启用" if srv["enabled"] else "✗ 停用"
        ro = "只读" if srv["read_only"] else "读写"
        tools = srv.get("tools", [])
        tool_names = ", ".join(t["remote_name"] for t in tools) if tools else "（暂未加载或无工具）"
        print(f"  {idx}. [{state}] {srv['name']} ({ro} · 命令: {srv['command']})")
        print(f"     暴露工具: {tool_names}")


def _resolve_mcp_server_token(token: str, statuses: list[dict]) -> str:
    if token.isdigit():
        idx = int(token)
        if not 1 <= idx <= len(statuses):
            raise ValueError(f"MCP Server 编号超出范围（请输入 1 到 {len(statuses)}）。")
        return statuses[idx - 1]["name"]
    for srv in statuses:
        if srv["name"] == token:
            return srv["name"]
    raise ValueError(f"未找到 MCP Server：{token}")


def _apply_mcp_choice(
    choice: str,
    statuses: list[dict],
    mcp_manager: McpManager,
    tool_registry: ToolRegistry | None,
) -> None:
    del tool_registry
    tokens = choice.strip().split()
    if not tokens:
        return
    head = tokens[0].casefold()
    try:
        if head in {"on", "enable", "off", "disable"}:
            if len(tokens) < 2:
                raise ValueError(f"请指定 MCP Server 编号或名称，例如：{head} 1")
            target_state = head in {"on", "enable"}
            for raw in tokens[1:]:
                name = _resolve_mcp_server_token(raw, statuses)
                mcp_manager.set_server_enabled(name, target_state)
                print(f"已{'启用' if target_state else '停用'} MCP Server：{name}。")
            return

        targets = tokens[1:] if head in {"t", "toggle"} else tokens
        for raw in targets:
            name = _resolve_mcp_server_token(raw, statuses)
            new_state = mcp_manager.toggle_server(name)
            print(f"已{'启用' if new_state else '停用'} MCP Server：{name}。")
    except ValueError as error:
        print(f"MCP 操作失败：{error}", file=sys.stderr)


def _manage_task(
    line: str,
    workspace: Path,
    *,
    read_input: Callable[[str], str] | None = None,
) -> None:
    parts = line.split(maxsplit=2)
    identity = project_identity(workspace)
    store = TaskStateStore(state_root() / "projects" / identity / "task.json")
    action = parts[1] if len(parts) > 1 else "view"
    try:
        if action in {"help", "?"}:
            print("用法：/task view|goal <目标>|verify <验证命令>|requirement|decision|progress|open <内容>|clear")
        elif action == "view":
            state = store.load()
            lines = [f"目标：{state.goal or '（未设置）'}"]
            if state.plan_steps:
                lines.append("计划步骤：")
                for idx, step in enumerate(state.plan_steps, start=1):
                    lines.append(f"  {idx}. [{step.status}] {step.title}")
            if state.verification_command:
                lines.append(f"验证命令：{state.verification_command}")
            for title, values in (
                ("已确认要求", state.requirements),
                ("已确认决定", state.decisions),
                ("进度", state.progress),
                ("待解决", state.open_questions),
            ):
                lines.append(f"{title}：" + ("；".join(values) if values else "（无）"))
            if _use_tui_menu(read_input):
                run_interactive_menu(
                    title="当前任务状态 (/task)",
                    subtitle="退出后自动擦除不污染聊天",
                    items_provider=lambda: [MenuItem(str(i), l) for i, l in enumerate(lines)],
                    footer_hints=(("Esc/q/Enter", "关闭"),),
                    erase_when_done=True,
                )
            else:
                print("\n".join(lines))
        elif action == "goal":
            if len(parts) < 3:
                raise ValueError("用法：/task goal <目标>")
            store.set_goal(parts[2])
            if not _use_tui_menu(read_input):
                print("已更新当前任务目标。")
        elif action == "verify":
            if len(parts) < 3:
                raise ValueError("用法：/task verify <验证命令>")
            store.update_plan(verification_command=parts[2])
            if not _use_tui_menu(read_input):
                print("已更新任务验证命令。")
        elif action == "clear":
            store.clear()
            if not _use_tui_menu(read_input):
                print("已清除当前任务状态。")
        else:
            if len(parts) < 3:
                raise ValueError("请提供状态内容。输入 /task help 查看用法。")
            store.add(action, parts[2])
            if not _use_tui_menu(read_input):
                print("已记录当前任务状态。")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"任务状态操作失败：{error}", file=sys.stderr)


def _show_context_trace(*, read_input: Callable[[str], str] | None = None) -> None:
    path = state_root() / "context-traces.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        print(f"无法读取 Context 记录：{error}", file=sys.stderr)
        return
    identity = project_identity(Path.cwd())
    trace = None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("project_id") == identity:
            trace = candidate
            break
    if trace is None:
        if not _use_tui_menu(read_input):
            print("还没有 Context 请求记录。")
        return
    summary_lines = [
        f"模式：{trace.get('mode', 'normal')}；估算输入：{trace.get('estimated_tokens')} tokens；"
        f"实际输入：{trace.get('actual_input_tokens')}；实际输出：{trace.get('actual_output_tokens')}",
        f"裁剪消息：{trace.get('trimmed_messages')}；生成摘要：{'是' if trace.get('summarized') else '否'}；"
        f"预算超出：{trace.get('budget_overflow_tokens', 0)} tokens",
    ]
    for source in trace.get("sources", []):
        summary_lines.append("- " + json.dumps(source, ensure_ascii=False))
    if _use_tui_menu(read_input):
        run_interactive_menu(
            title="Context 来源与预算记录 (/context)",
            subtitle="退出后自动擦除不污染聊天",
            items_provider=lambda: [MenuItem(str(i), l) for i, l in enumerate(summary_lines)],
            footer_hints=(("Esc/q/Enter", "关闭"),),
            erase_when_done=True,
        )
    else:
        print("\n".join(summary_lines))


def _print_memory(item) -> None:
    print(
        f"{item.id[:8]} [{item.scope}/{item.category}] 来源={item.source_type} "
        f"创建={item.created_at} 更新={item.updated_at} 历史版本={len(item.history or [])} {item.content}"
    )
