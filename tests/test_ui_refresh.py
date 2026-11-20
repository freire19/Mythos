"""UI refresh (Nov 2026): transcript layout, permission menu, scroll region.

Covers the pieces that changed shape:

- scroll-region setup/teardown keep the cursor INSIDE the region (the
  hidden `ask_choice` menu bug: output collapsing onto the bottom row);
- `● Tool(args)` headers with display names + cwd-relative paths;
- `⎿` results: bulk summaries, trimmed (never terminal-wrapped) lines;
- parallel batches: every result sits under its own header;
- numbered permission menu (digits and the legacy letters);
- ANSI-aware wrapping of assistant prose.
"""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout

import pytest

from alpha.display import core as display_core
from alpha.display import thinking as thinking_mod
from alpha.display.markdown import render_markdown, visible_len, wrap_ansi
from alpha.display.renderers import prompts as prompts_mod
from alpha.display.renderers import tools as tools_mod

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[78]")


def _plain(s: str) -> str:
    return _ANSI.sub("", s)


def _capture(fn, *a, **kw) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **kw)
    return _plain(buf.getvalue())


# ─── scroll region ──────────────────────────────────────────────────


class TestScrollRegionCursor:
    def _indicator(self, monkeypatch, rows=30, cols=100):
        ind = thinking_mod.ThinkingIndicator()
        ind._enabled = True
        monkeypatch.setattr(ind, "_scroll_supported", lambda: True)
        monkeypatch.setattr(ind, "_detect_size", lambda: (rows, cols))
        return ind

    def test_setup_moves_relative_then_sets_region(self, monkeypatch):
        ind = self._indicator(monkeypatch)
        buf = io.StringIO()
        monkeypatch.setattr(thinking_mod.sys, "stdout", buf)
        assert ind._setup_scroll() is True
        out = buf.getvalue()
        reserved = ind._BASE_RESERVED
        # Room is made with newlines + cursor-up (relative), so a cursor
        # that started on the last row ends at the region's bottom row —
        # not below it, where LF would stop scrolling.
        assert out.startswith("\n" * reserved + f"\033[{reserved}A")
        assert f"\033[1;{30 - reserved}r" in out
        # DECSTBM homes the cursor: it must be wrapped in save/restore.
        assert out.index("\0337") < out.index("r") < out.index("\0338")

    def test_teardown_restores_cursor_instead_of_jumping_to_bottom(self, monkeypatch):
        ind = self._indicator(monkeypatch)
        buf = io.StringIO()
        monkeypatch.setattr(thinking_mod.sys, "stdout", buf)
        ind._setup_scroll()
        buf.seek(0)
        buf.truncate()
        ind._teardown_scroll()
        out = buf.getvalue()
        assert out.startswith("\0337") and out.endswith("\033[r\0338")
        assert ind.is_scroll_active() is False


# ─── headers ────────────────────────────────────────────────────────


class TestToolHeader:
    def test_display_names(self):
        assert tools_mod._display_tool_name("read_file") == "Read"
        assert tools_mod._display_tool_name("execute_shell") == "Bash"
        assert tools_mod._display_tool_name("mcp__github__list_prs") == "github:list_prs"
        assert tools_mod._display_tool_name("some_new_tool") == "some_new_tool"

    def test_path_shown_relative_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        out = _capture(
            tools_mod.print_tool_call, "read_file",
            {"path": str(tmp_path / "pkg" / "mod.py")},
        )
        assert out.strip() == "● Read(pkg/mod.py)"

    def test_header_never_exceeds_terminal_width(self, monkeypatch):
        monkeypatch.setattr(
            "alpha.display.theme.term_cols", lambda default=80: 60
        )
        out = _capture(
            tools_mod.print_tool_call, "execute_shell", {"command": "x" * 300},
            "destructive",
        )
        assert max(len(ln) for ln in out.splitlines()) < 60

    def test_ask_choice_draws_no_header_or_result(self):
        assert _capture(tools_mod.print_tool_call, "ask_choice", {"question": "q"}) == ""
        assert _capture(
            tools_mod.print_tool_result, "ask_choice", {"ok": True, "chosen_value": "a"}
        ) == ""


# ─── results ────────────────────────────────────────────────────────


class TestToolResult:
    def test_read_file_collapses_to_line_count(self):
        out = _capture(
            tools_mod.print_tool_result, "read_file",
            {"content": "1: a\n2: b\n3: c", "total_lines": 3},
        )
        assert out.strip() == "⎿  Read 3 lines"

    def test_read_file_partial(self):
        out = _capture(
            tools_mod.print_tool_result, "read_file",
            {"content": "1: a\n2: b", "total_lines": 200},
        )
        assert "Read 2 of 200 lines" in out

    def test_list_and_search_summaries(self):
        assert "Listed 12 paths" in _capture(
            tools_mod.print_tool_result, "list_directory",
            {"path": "/x", "count": 12, "entries": [{}]},
        )
        assert "Found 1 match" in _capture(
            tools_mod.print_tool_result, "search_files",
            {"matches": 1, "results": [{}]},
        )

    def test_shell_output_keeps_leading_space_and_caps_lines(self):
        stdout = "\n".join(f" M file{i}.py" for i in range(9))
        out = _capture(tools_mod.print_tool_result, "execute_shell", {"stdout": stdout})
        lines = out.splitlines()
        assert lines[0] == "  ⎿   M file0.py"
        assert lines[1] == "      M file1.py"
        assert lines[-1].strip() == "… +4 lines"
        assert "ctrl+o" not in out  # no hint for a shortcut that doesn't exist

    def test_error_line(self):
        out = _capture(tools_mod.print_tool_result, "read_file", {"error": "nope"})
        assert out.strip() == "⎿  Error: nope"

    def test_success_flag_without_output_reads_done(self):
        out = _capture(tools_mod.print_tool_result, "pre_flight", {"ok": True})
        assert out.strip() == "⎿  Done"

    def test_edit_diff_uses_real_line_numbers(self, tmp_path):
        f = tmp_path / "calc.py"
        f.write_text("a = 1\nb = 2\nc = 3\nd = 40\n")
        out = _capture(
            tools_mod.print_tool_result, "edit_file",
            {"path": str(f)},
            {"path": str(f), "old_text": "d = 4", "new_text": "d = 40"},
        )
        assert "with 1 addition and 1 removal" in out
        assert "   4 + d = 40" in out
        assert "   4 - d = 4" in out


# ─── parallel batches ───────────────────────────────────────────────


class TestBatchPairing:
    def test_each_result_sits_under_its_own_header(self, monkeypatch):
        from alpha.cli import entry

        log: list[str] = []
        monkeypatch.setattr(entry, "ThinkingIndicator", lambda *a, **k: _Ind())
        monkeypatch.setattr(entry, "erase_active_indicator_line", lambda: None)
        monkeypatch.setattr(
            entry, "print_tool_call", lambda n, a, s="safe": log.append(f"H:{a['path']}")
        )
        monkeypatch.setattr(
            entry, "print_tool_result",
            lambda n, r, args=None: log.append(f"R:{args['path']}"),
        )
        st = entry._TurnState()
        for cid, path in (("c1", "a.py"), ("c2", "b.py")):
            st.on_tool_call({"id": cid, "name": "read_file", "args": {"path": path}})
        for cid in ("c1", "c2"):
            st.on_tool_result({"id": cid, "name": "read_file", "result": {"content": ""}})
        assert log == ["H:a.py", "R:a.py", "H:b.py", "R:b.py"]


class _Ind:
    def start(self, label=None): pass
    def stop(self): pass
    def pause(self): pass
    def add_streamed_text(self, text): pass


# ─── permission menu ────────────────────────────────────────────────


class TestPermissionMenu:
    @pytest.fixture(autouse=True)
    def _reset(self):
        prompts_mod.set_auto_accept(False)
        yield
        prompts_mod.set_auto_accept(False)

    @pytest.mark.parametrize("answer,expected", [
        ("1", True), ("s", True), ("4", False), ("n", False),
    ])
    def test_digits_and_letters(self, monkeypatch, capsys, answer, expected):
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
        ok = prompts_mod.print_approval_request("execute_shell", {"command": "ls"})
        assert ok is expected
        out = _plain(capsys.readouterr().out)
        assert "Permissão necessária" in out
        assert "command: ls" in out
        assert "1. Sim" in out and "4. Não" in out

    def test_three_turns_on_session_blanket(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt="": "3")
        assert prompts_mod.print_approval_request("execute_shell", {"command": "ls"})
        assert prompts_mod.is_auto_accept() is True

    def test_ask_choice_needs_no_approval(self):
        from alpha.approval import needs_approval

        assert needs_approval("ask_choice", {"question": "q", "options": ["a"]}) is False


# ─── prose ──────────────────────────────────────────────────────────


class TestProse:
    def test_wrap_ansi_hanging_indent_and_style_carry(self):
        line = "  1. \x1b[1mnome\x1b[0m " + "palavra " * 12 + "\x1b[1mnegrito longo aqui\x1b[0m fim"
        rows = wrap_ansi(line, 40)
        assert len(rows) > 2
        assert all(visible_len(r) <= 40 for r in rows)
        assert all(_plain(r).startswith("     ") for r in rows[1:])
        # every row is self-contained: a style opened on it is closed on it
        for r in rows:
            assert r.count("\x1b[1m") <= r.count("\x1b[0m")

    def test_wrap_ansi_short_line_untouched(self):
        assert wrap_ansi("curto", 40) == ["curto"]

    def test_assistant_text_gets_bullet_and_indent(self):
        out = _plain(display_core.format_assistant_text("um\ndois\n"))
        assert out == "\n● um\n  dois\n"

    def test_user_echo_wraps_long_message(self, monkeypatch):
        monkeypatch.setattr(display_core, "term_cols", lambda default=80: 40)
        out = _plain(display_core.format_user_echo("palavra " * 20))
        rows = [r for r in out.split("\n") if r]
        assert rows[0].startswith(" ❯ palavra")
        assert len(rows) > 1 and all(len(r) <= 40 for r in rows)

    def test_fenced_code_is_verbatim(self, monkeypatch):
        import alpha.display.markdown as md

        monkeypatch.setattr(md, "NO_COLOR", False)
        out = _plain(render_markdown("antes\n```python\nx = a * b  # **nao** é negrito\n```\ndepois"))
        assert "```" not in out
        assert "x = a * b  # **nao** é negrito" in out

    def test_chat_only_turn_prints_no_summary(self, capsys):
        display_core.print_turn_summary(0, 1.2)
        assert capsys.readouterr().out == ""


# ── arrow-key menu ──────────────────────────────────────────────────


def _run_menu(keys: bytes, options, shortcuts=None):
    """Run select_menu against a real pty, typing `keys` once the menu is up
    (input typed before it opens is flushed on purpose)."""
    import io
    import os as _os
    import pty
    import threading
    import time

    from alpha.display import select as select_mod

    master, slave = pty.openpty()
    out = io.StringIO()

    def _type():
        deadline = time.monotonic() + 5
        while "Enter" not in out.getvalue() and time.monotonic() < deadline:
            time.sleep(0.005)
        _os.write(master, keys)

    class _In:
        def fileno(self):
            return slave

    typist = threading.Thread(target=_type, daemon=True)
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _In(), out
    try:
        typist.start()
        result = select_mod.select_menu(options, shortcuts=shortcuts)
        return result, out.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
        typist.join(timeout=5)
        _os.close(master)
        _os.close(slave)


class TestSelectMenu:
    OPTS = [["Sim"], ["Sim, sempre"], ["Sessão"], ["Não"]]

    def test_arrows_move_and_enter_confirms(self):
        result, out = _run_menu(b"\x1b[B\x1b[B\r", self.OPTS)
        assert result == 2
        assert "❯" in out

    def test_application_mode_arrows(self):
        assert _run_menu(b"\x1bOB\x1bOA\x1bOB\r", self.OPTS)[0] == 1

    def test_up_wraps_to_last(self):
        assert _run_menu(b"\x1b[A\r", self.OPTS)[0] == 3

    def test_digit_selects_immediately(self):
        assert _run_menu(b"4", self.OPTS)[0] == 3

    def test_out_of_range_digit_is_ignored(self):
        assert _run_menu(b"9\r", self.OPTS)[0] == 0

    def test_letter_shortcut(self):
        assert _run_menu(b"T", self.OPTS, shortcuts={"t": 2})[0] == 2

    def test_escape_cancels(self):
        assert _run_menu(b"\x1b", self.OPTS)[0] is None

    def test_ctrl_c_raises(self):
        with pytest.raises(KeyboardInterrupt):
            _run_menu(b"\x03", self.OPTS)

    def test_two_digit_choice_needs_enter(self):
        opts = [[f"opção {i}"] for i in range(1, 13)]
        assert _run_menu(b"12\r", opts)[0] == 11

    def test_approval_falls_back_to_typed_answer_without_tty(self, monkeypatch, capsys):
        from alpha.display.renderers import prompts as prompts_mod

        monkeypatch.setattr(prompts_mod, "_approve_all", False)
        monkeypatch.setattr(prompts_mod, "can_select", lambda: False)
        monkeypatch.setattr("builtins.input", lambda *_: "4")
        assert prompts_mod.print_approval_request("execute_shell", {"command": "ls"}) is False
        assert "4." in capsys.readouterr().out


# ── sub-agent trace ─────────────────────────────────────────────────


class TestSubagentCompactTrace:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv("ALPHA_SUBAGENT_TRACE", raising=False)
        display_core._subagent_activity.clear()
        yield
        display_core._subagent_activity.clear()

    def _call(self, label, path):
        display_core.print_subagent_event(
            {"type": "tool_call", "name": "read_file", "args": {"path": path}, "safety": "safe"},
            agent_label=label,
        )

    def test_calls_are_counted_not_printed(self, capsys):
        for i in range(20):
            self._call("#1", f"mod{i}.py")
        assert capsys.readouterr().out == ""
        assert display_core._subagent_activity == {"#1": 20}

    def test_spinner_shows_agents_count_and_latest_call(self, monkeypatch):
        labels = []

        class _Ind:
            def update_label(self, text):
                labels.append(text)

        monkeypatch.setattr(thinking_mod, "get_active_indicator", lambda: _Ind())
        monkeypatch.setenv("COLUMNS", "120")
        self._call("#1", "a.py")
        self._call("#2", "b.py")
        self._call("#2", "c.py")
        assert labels[0] == "1 tool use · #1 Read(a.py)"
        assert labels[-1] == "2 agents · 3 tool uses · #2 Read(c.py)"

    def test_finished_agent_leaves_the_counter(self):
        self._call("#1", "a.py")
        display_core.flush_subagent_dup("#1")
        assert display_core._subagent_activity == {}

    def test_parallel_header_shows_agent_count(self):
        header = tools_mod._format_tool_call_header(
            "delegate_parallel", {"tasks": ["analise A " * 30, "analise B", "analise C"]}, "destructive"
        )
        assert "Tasks" in header and "(3 agents)" in header
        assert "analise" not in header
        assert tools_mod.live_label_for_tool(
            "delegate_parallel", {"tasks": '["a", "b"]'}
        ) == "Delegating 2 agents"
