import os
import select
import time
from unittest import TestCase, skipUnless

from src.client.terminal_chat import PermissionModeController, TerminalInput


@skipUnless(os.name == "posix" and hasattr(os, "forkpty"), "requires a POSIX pseudo-terminal")
class TerminalInputTests(TestCase):
    def test_prompt_edits_unicode_and_shift_tab_toggles_permission_mode(self):
        pid, terminal = os.forkpty()
        if pid == 0:
            try:
                import fcntl, struct, termios
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
                mode = PermissionModeController()
                line = TerminalInput(mode).read()
                print(f"\nMODE={mode.mode}; RESULT={line}", flush=True)
            finally:
                os._exit(0)

        try:
            output = bytearray()
            deadline = time.monotonic() + 5
            while "›".encode("utf-8") not in output:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "terminal prompt did not appear")
                readable, _, _ = select.select([terminal], [], [], remaining)
                if readable:
                    output.extend(os.read(terminal, 4096))

            os.write(terminal, "\x1b[Z任务错bc".encode("utf-8") + b"\x7f\x08\r")
            while b"RESULT=" not in output:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "terminal input did not finish")
                readable, _, _ = select.select([terminal], [], [], remaining)
                if readable:
                    try:
                        output.extend(os.read(terminal, 4096))
                    except OSError:
                        break

            result = output.decode("utf-8", errors="replace")
            self.assertIn("MODE=auto; RESULT=任务错", result)
        finally:
            try:
                os.close(terminal)
            except OSError:
                pass
            os.waitpid(pid, 0)

    def test_tui_selector_navigates_with_arrow_keys_and_toggles_items_in_pty(self):
        from src.client.tui_selector import MenuItem, run_interactive_menu

        pid, terminal = os.forkpty()
        if pid == 0:
            try:
                state = {"read": True, "bash": True}

                def build():
                    return [
                        MenuItem("read", "read", enabled=state["read"], tag="只读"),
                        MenuItem("bash", "bash", enabled=state["bash"], tag="需审批"),
                    ]

                def on_act(action, item, _idx):
                    if action in {"space", "enter"} and item is not None:
                        state[item.key] = not state[item.key]
                        return f"Toggled {item.key}"
                    return None

                run_interactive_menu(
                    title="Tools Menu",
                    items_provider=build,
                    footer_hints=(("↑/↓", "move"), ("Space", "toggle"), ("q", "quit")),
                    on_key_action=on_act,
                )
                print(f"\nTUI_STATE=read:{state['read']},bash:{state['bash']}", flush=True)
            finally:
                os._exit(0)

        try:
            output = bytearray()
            deadline = time.monotonic() + 5
            while b"Tools Menu" not in output:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "TUI menu did not appear")
                readable, _, _ = select.select([terminal], [], [], remaining)
                if readable:
                    output.extend(os.read(terminal, 4096))

            # Press Down arrow (\x1b[B) to select 'bash', Space (' ') to toggle it off, then 'q' to quit
            os.write(terminal, b"\x1b[B q")
            while b"TUI_STATE=" not in output:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "TUI menu did not finish")
                readable, _, _ = select.select([terminal], [], [], remaining)
                if readable:
                    try:
                        output.extend(os.read(terminal, 4096))
                    except OSError:
                        break

            result = output.decode("utf-8", errors="replace")
            self.assertIn("TUI_STATE=read:True,bash:False", result)
        finally:
            try:
                os.close(terminal)
            except OSError:
                pass
            os.waitpid(pid, 0)

    def test_slash_command_completer_suggests_commands_with_descriptions(self):
        from prompt_toolkit.document import Document
        from src.client.tui_selector import SlashCommandCompleter

        completer = SlashCommandCompleter()
        completions = list(completer.get_completions(Document("/me", cursor_position=3), None))
        self.assertEqual([c.text for c in completions], ["/memory"])
        all_cmds = [c.text for c in completer.get_completions(Document("/", cursor_position=1), None)]
        self.assertTrue({"/memory", "/tools", "/skill", "/mcp", "/plan", "/goal", "/expand"}.issubset(set(all_cmds)))

    def test_persistent_bottom_toolbar_shows_full_model_name_and_ctrl_o_toggles_tool_expand(self):
        from src.client.terminal_chat import (
            PermissionModeController,
            TerminalInput,
            ToolDisplayController,
            _print_tool_result,
            _print_tool_start,
        )
        from src.harness.tools.base import ToolCall, ToolResult
        import io
        from contextlib import redirect_stdout

        mode = PermissionModeController()
        display = ToolDisplayController(expanded=False)
        models = {"glm": "智谱 GLM / glm-5.3-flash"}
        term_in = TerminalInput(
            mode,
            active_model="glm",
            models=models,
            mode_getter=lambda: "plan",
            tool_display=display,
        )
        toolbar_text = "".join(frag[1] for frag in term_in._bottom_toolbar())
        self.assertIn("Default（逐项审批）", toolbar_text)
        self.assertIn("模式: PLAN", toolbar_text)
        self.assertIn("智谱 GLM / glm-5.3-flash [glm]", toolbar_text)
        self.assertIn("工具详情: 折叠", toolbar_text)

        # Prompt message itself only contains the clean frame and '› ', not the status clutter
        prompt_text = "".join(frag[1] for frag in term_in._prompt_message())
        self.assertIn("╭─", prompt_text)
        self.assertIn("› ", prompt_text)
        self.assertNotIn("Default（逐项审批）", prompt_text)

        # Collapsed tool output prints only a single compact summary line
        call = ToolCall("c1", "read", '{"path":"README.md"}')
        res = ToolResult("c1", "line 1\nline 2\nline 3")
        buf_collapsed = io.StringIO()
        with redirect_stdout(buf_collapsed):
            _print_tool_start(call, {"path": "README.md"}, color=True, display=display)
            _print_tool_result(call, res, color=True, display=display)
        collapsed_out = buf_collapsed.getvalue()
        self.assertIn('read(path="README.md")', collapsed_out)
        self.assertIn("共 3 行", collapsed_out)
        self.assertNotIn("line 2", collapsed_out)

        # Toggle expand (Ctrl+O / /expand) -> prints full panel with all lines
        display.toggle()
        self.assertTrue(display.expanded)
        buf_expanded = io.StringIO()
        with redirect_stdout(buf_expanded):
            _print_tool_start(call, {"path": "README.md"}, color=True, display=display)
            _print_tool_result(call, res, color=True, display=display)
        expanded_out = buf_expanded.getvalue()
        self.assertIn("line 2", expanded_out)
