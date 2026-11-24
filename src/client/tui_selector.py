"""Interactive TUI selector widget supporting Up/Down arrow keys, mouse click, hotkeys, and zero-pollution erase."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import sys
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.shortcuts import prompt as pt_prompt
from prompt_toolkit.styles import Style


TUI_STYLE = Style.from_dict(
    {
        "frame.border": "#3b82f6",
        "title": "bold #38bdf8",
        "subtitle": "#94a3b8",
        "cursor": "bold #38bdf8",
        "item": "#e2e8f0",
        "item.selected": "bold #0f172a bg:#38bdf8",
        "badge.on": "bold #22c55e",
        "badge.on.selected": "bold #052e16 bg:#38bdf8",
        "badge.off": "#64748b",
        "badge.off.selected": "#1e293b bg:#38bdf8",
        "badge.tag": "#a78bfa",
        "badge.tag.selected": "bold #1e1b4b bg:#38bdf8",
        "meta": "#94a3b8",
        "meta.selected": "#0f172a bg:#38bdf8",
        "footer": "#94a3b8",
        "footer.key": "bold #38bdf8",
        "status": "bold #f59e0b",
        # Completion menu
        "completion-menu.completion": "bg:#1e293b #e2e8f0",
        "completion-menu.completion.current": "bg:#38bdf8 #0f172a bold",
        "completion-menu.meta.completion": "bg:#1e293b #94a3b8",
        "completion-menu.meta.completion.current": "bg:#38bdf8 #0f172a",
        # Claude Code style input frame & bottom toolbar
        "prompt.box": "#475569",
        "prompt.arrow": "bold #38bdf8",
        "bottom-toolbar": "bg:#0f172a #cbd5e1",
        "toolbar.perm.default": "bg:#0f172a bold #38bdf8",
        "toolbar.perm.auto": "bg:#0f172a bold #22c55e",
        "toolbar.mode.normal": "bg:#0f172a #94a3b8",
        "toolbar.mode.plan": "bg:#0f172a bold #a78bfa",
        "toolbar.mode.goal": "bg:#0f172a bold #f59e0b",
        "toolbar.model": "bg:#0f172a bold #e2e8f0",
        "toolbar.tools": "bg:#0f172a #94a3b8",
        "toolbar.sep": "bg:#0f172a #475569",
    }
)


SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/memory", "交互式查看、修改、删除或搜索全局与项目长期记忆（退出后自动擦除不留痕）"),
    ("/tools", "交互式查看、开启或关闭内置与扩展工具（退出后自动擦除不留痕）"),
    ("/skill", "交互式查看、启停、预览或调用已安装 Skill（退出后自动擦除不留痕）"),
    ("/mcp", "交互式查看、开启或关闭已配置 MCP Server（退出后自动擦除不留痕）"),
    ("/expand", "展开或折叠工具调用的详细参数与输出（快捷键 Ctrl+O）"),
    ("/plan", "切换到 Plan 只读规划模式（生成结构化实施计划）"),
    ("/goal", "切换到 Goal 自主长任务模式（按计划步骤自动推进并验证）"),
    ("/mode", "切换运行模式（normal | plan | goal）"),
    ("/task", "查看或维护当前项目任务目标、计划步骤与验证命令"),
    ("/model", "交互式选择并切换当前对话模型"),
    ("/hitl", "切换工具审批权限模式（default 逐项审批 | auto 免审）"),
    ("/resume", "从上次未完成的断点（checkpoint.json）恢复执行"),
    ("/context", "查看最近一次模型请求的 Context 装配来源与 Token 用量"),
    ("/new", "归档当前对话并开始新聊天"),
    ("/retry", "从当前位置重试失败的模型请求"),
    ("/cancel", "中止当前正在进行的轮次"),
)


class SlashCommandCompleter(Completer):
    """Auto-complete slash commands when the user types '/' at the start of the line."""

    def get_completions(self, document, complete_event):
        del complete_event
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/") or " " in text:
            return
        query = text.casefold()
        for cmd, desc in SLASH_COMMANDS:
            if cmd.startswith(query):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


@dataclass
class MenuItem:
    key: str
    label: str
    enabled: bool | None = None
    tag: str = ""
    meta: str = ""
    payload: Any = None


@dataclass
class MenuOutcome:
    action: str
    index: int
    item: MenuItem | None


def is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def transient_input(prompt_label: str, default: str = "") -> str:
    """Prompt for a short input and immediately erase the prompt line from the terminal when done."""
    if not is_interactive_tty():
        return input(f"{prompt_label} › ").strip()
    try:
        value = pt_prompt(
            [("class:title", f"  ✎ {prompt_label} › ")],
            default=default,
            style=TUI_STYLE,
            erase_when_done=True,
        )
        return value.strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def run_interactive_menu(
    *,
    title: str,
    subtitle: str = "",
    items_provider: Callable[[], list[MenuItem]],
    footer_hints: tuple[tuple[str, str], ...],
    on_key_action: Callable[[str, MenuItem, int], str | MenuOutcome | None] | None = None,
    initial_index: int = 0,
    erase_when_done: bool = True,
    input_stream=None,
    output_stream=None,
) -> MenuOutcome | None:
    """Run an interactive keyboard (Up/Down/Enter/Space/hotkeys) and mouse-clickable menu.

    By default ``erase_when_done=True`` so slash-command menus disappear cleanly when closed
    and never pollute the conversation transcript above.
    """
    items = items_provider()
    selected = max(0, min(initial_index, max(0, len(items) - 1)))
    status_message = ""
    detail_text = ""

    def refresh_items() -> None:
        nonlocal items, selected
        items = items_provider()
        if not items:
            selected = 0
        else:
            selected = max(0, min(selected, len(items) - 1))

    def trigger(action_name: str, app: Application) -> None:
        nonlocal status_message, detail_text
        if detail_text and action_name not in {"escape", "q"}:
            detail_text = ""
            return
        if not items and action_name not in {"0", "s"}:
            app.exit(result=None)
            return
        current_item = items[selected] if items else None
        if on_key_action is not None:
            outcome = on_key_action(action_name, current_item, selected)  # type: ignore[arg-type]
            if isinstance(outcome, MenuOutcome):
                app.exit(result=outcome)
                return
            if isinstance(outcome, str):
                if outcome.startswith("__DETAIL__:"):
                    detail_text = outcome[len("__DETAIL__:") :]
                else:
                    status_message = outcome
                refresh_items()
                return
        app.exit(result=MenuOutcome(action_name, selected, current_item))

    def render() -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        fragments.append(("class:frame.border", "╭─ "))
        fragments.append(("class:title", title))
        if subtitle:
            fragments.append(("class:subtitle", f" · {subtitle}"))
        fragments.append(("class:frame.border", " ─╮\n"))

        if detail_text:
            for line in detail_text.splitlines()[:24]:
                fragments.append(("class:item", f"  {line}\n"))
            fragments.append(("class:status", "  ✦ 按任意键返回列表，或按 Esc/q 退出\n"))
            fragments.append(("class:frame.border", "╰────────────────────────────────────────╯"))
            return fragments

        if not items:
            fragments.append(("class:meta", "  （当前列表为空）\n"))
        else:
            for idx, item in enumerate(items):
                is_sel = idx == selected

                def make_mouse_handler(row_idx: int = idx):
                    def _handle(mouse_event: MouseEvent):
                        nonlocal selected
                        if mouse_event.event_type == MouseEventType.MOUSE_UP:
                            if selected == row_idx:
                                from prompt_toolkit.application.current import get_app

                                trigger("enter", get_app())
                            else:
                                selected = row_idx
                                return None
                        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                            selected = min(len(items) - 1, selected + 1)
                        elif mouse_event.event_type == MouseEventType.SCROLL_UP:
                            selected = max(0, selected - 1)
                        return None

                    return _handle

                handler = make_mouse_handler(idx)
                prefix = "  ❯ " if is_sel else "    "
                row_style = "class:item.selected" if is_sel else "class:item"
                fragments.append((row_style, f"{prefix}{idx + 1:>2}. ", handler))

                if item.enabled is not None:
                    if item.enabled:
                        b_style = "class:badge.on.selected" if is_sel else "class:badge.on"
                        fragments.append((b_style, "[● 启用] ", handler))
                    else:
                        b_style = "class:badge.off.selected" if is_sel else "class:badge.off"
                        fragments.append((b_style, "[○ 停用] ", handler))

                if item.tag:
                    t_style = "class:badge.tag.selected" if is_sel else "class:badge.tag"
                    fragments.append((t_style, f"[{item.tag}] ", handler))

                fragments.append((row_style, item.label, handler))
                if item.meta:
                    m_style = "class:meta.selected" if is_sel else "class:meta"
                    fragments.append((m_style, f"  {item.meta}", handler))
                fragments.append(("", "\n"))

        if status_message:
            fragments.append(("class:status", f"  ✦ {status_message}\n"))

        fragments.append(("class:frame.border", "╰─ "))
        for idx, (k, desc) in enumerate(footer_hints):
            if idx > 0:
                fragments.append(("class:footer", " · "))
            fragments.append(("class:footer.key", k))
            fragments.append(("class:footer", f" {desc}"))
        fragments.append(("class:frame.border", " ─╯"))
        return fragments

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event) -> None:
        nonlocal selected, detail_text
        if detail_text:
            detail_text = ""
            return
        if items:
            selected = (selected - 1) % len(items)

    @kb.add("down")
    @kb.add("j")
    def _down(event) -> None:
        nonlocal selected, detail_text
        if detail_text:
            detail_text = ""
            return
        if items:
            selected = (selected + 1) % len(items)

    @kb.add("enter")
    def _enter(event) -> None:
        trigger("enter", event.app)

    @kb.add("space")
    def _space(event) -> None:
        trigger("space", event.app)

    @kb.add("e")
    def _edit(event) -> None:
        trigger("e", event.app)

    @kb.add("d")
    @kb.add("delete")
    @kb.add("backspace")
    def _delete(event) -> None:
        trigger("d", event.app)

    @kb.add("h")
    def _history(event) -> None:
        trigger("h", event.app)

    @kb.add("r")
    def _resolve(event) -> None:
        trigger("r", event.app)

    @kb.add("v")
    def _view(event) -> None:
        trigger("v", event.app)

    @kb.add("u")
    def _use(event) -> None:
        trigger("u", event.app)

    @kb.add("t")
    def _toggle(event) -> None:
        trigger("t", event.app)

    @kb.add("s")
    def _search(event) -> None:
        trigger("s", event.app)

    @kb.add("0")
    def _zero(event) -> None:
        trigger("0", event.app)

    @kb.add("a")
    def _all(event) -> None:
        trigger("a", event.app)

    @kb.add("escape")
    @kb.add("q")
    @kb.add("c-c")
    @kb.add("c-d")
    def _quit(event) -> None:
        nonlocal detail_text
        if detail_text:
            detail_text = ""
            return
        event.app.exit(result=None)

    control = FormattedTextControl(render, focusable=True, show_cursor=False)
    app: Application[MenuOutcome | None] = Application(
        layout=Layout(HSplit([Window(content=control, always_hide_cursor=True)])),
        key_bindings=kb,
        style=TUI_STYLE,
        mouse_support=True,
        full_screen=False,
        erase_when_done=erase_when_done,
        input=input_stream,
        output=output_stream,
    )
    return app.run()
