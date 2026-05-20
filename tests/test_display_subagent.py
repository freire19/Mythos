"""Regression tests for print_subagent_event re-export surface.

The smoke test (`python main.py "oi"`) never hits this code path because
`print_subagent_event` only runs from sub-agent events. Refactors that drop
a re-export from `core.py` therefore only fail in production. This file
forces those failures at test-load time.
"""

import pytest

from alpha.display import core as display_core


@pytest.fixture(autouse=True)
def _reset_subagent_state():
    display_core._subagent_last_call.clear()
    yield
    display_core._subagent_last_call.clear()


class TestPrintSubagentEventToolCall:
    def test_tool_call_renders_without_nameerror(self, capsys):
        display_core.print_subagent_event(
            {
                "type": "tool_call",
                "name": "read_file",
                "args": {"path": "alpha/agent/__init__.py"},
                "safety": "safe",
            },
            agent_label="agent-1",
        )
        out = capsys.readouterr().out
        assert "read_file" in out
        assert "⚤" in out

    def test_tool_call_destructive_safety(self, capsys):
        display_core.print_subagent_event(
            {
                "type": "tool_call",
                "name": "execute_shell",
                "args": {"command": "rm -rf /tmp/foo"},
                "safety": "destructive",
            },
            agent_label="agent-2",
        )
        out = capsys.readouterr().out
        assert "rm -rf" in out
        assert "⚤" in out

    def test_tool_call_without_agent_label(self, capsys):
        display_core.print_subagent_event(
            {
                "type": "tool_call",
                "name": "list_directory",
                "args": {"path": "."},
                "safety": "safe",
            },
        )
        assert "list_directory" in capsys.readouterr().out

    def test_duplicate_tool_call_is_collapsed(self, capsys):
        event = {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "x.py"},
            "safety": "safe",
        }
        display_core.print_subagent_event(event, agent_label="dup-agent")
        display_core.print_subagent_event(event, agent_label="dup-agent")
        display_core.print_subagent_event(event, agent_label="dup-agent")
        assert capsys.readouterr().out.count("read_file") == 1

    def test_flush_subagent_dup_emits_counter(self, capsys):
        event = {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "x.py"},
            "safety": "safe",
        }
        display_core.print_subagent_event(event, agent_label="dup-agent")
        display_core.print_subagent_event(event, agent_label="dup-agent")
        display_core.flush_subagent_dup("dup-agent")
        assert "×2" in capsys.readouterr().out


class TestPrintSubagentEventDone:
    def test_done_event_renders_reply(self, capsys):
        display_core.print_subagent_event(
            {"type": "done", "reply": "Task completed successfully."},
            agent_label="agent-1",
        )
        assert "Task completed successfully" in capsys.readouterr().out

    def test_done_with_empty_reply_is_silent(self, capsys):
        display_core.print_subagent_event({"type": "done", "reply": ""})
        assert capsys.readouterr().out == ""

    def test_done_flushes_pending_dup_counter(self, capsys):
        tool_event = {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "x.py"},
            "safety": "safe",
        }
        display_core.print_subagent_event(tool_event, agent_label="a")
        display_core.print_subagent_event(tool_event, agent_label="a")
        display_core.print_subagent_event(
            {"type": "done", "reply": "ok"}, agent_label="a"
        )
        out = capsys.readouterr().out
        assert "×2" in out
        assert "ok" in out


class TestReExportSurface:
    """Lock the symbols print_subagent_event reaches for into core's
    re-export list. A future refactor that drops one fails here instead
    of in production."""

    def test_format_tool_call_header_reexported(self):
        assert callable(display_core._format_tool_call_header)

    def test_print_result_body_reexported(self):
        assert callable(display_core._print_result_body)

    def test_color_primitives_reexported(self):
        assert callable(display_core.c)
        assert hasattr(display_core, "C")
