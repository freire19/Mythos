"""AUDIT_V2.2 #029: buffer/cursor state-machine coverage for repl_concurrent.

This module has a recurring history of regressions (scroll-region vs thinking
spinner, "input sobe + engole recap", CPR fallback) and had zero tests. The
ANSI rendering / scroll-region behaviour needs a real terminal (validated in
runtime), but the line-editing state machine (`_process_char` / `_process_escape`
mutating `_buffer`, `_cursor`, `_queue`) is pure logic and testable headless.

`_render_locked` is patched to a no-op so we exercise the buffer logic without
writing ANSI to the terminal."""

import pytest

from alpha import repl_concurrent as rc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Reset persistent buffer/cursor/queue and silence the terminal repaint.
    monkeypatch.setattr(rc, "_render_locked", lambda: None)
    rc._buffer = ""
    rc._cursor = 0
    rc._queue.clear()
    yield
    rc._buffer = ""
    rc._cursor = 0
    rc._queue.clear()


def _type(text: str) -> None:
    for ch in text:
        rc._process_char(ch)


class TestPrintableAndCursor:
    def test_typing_appends(self):
        _type("hello")
        assert rc._buffer == "hello"
        assert rc._cursor == 5

    def test_insert_mid_buffer(self):
        _type("helo")
        # move cursor left once (before the final 'o') and insert 'l'
        rc._process_escape("\x1b[D")
        rc._process_char("l")
        assert rc._buffer == "hello"

    def test_backspace_removes_left_of_cursor(self):
        _type("abc")
        rc._process_char("\x7f")
        assert rc._buffer == "ab"
        assert rc._cursor == 2

    def test_backspace_at_start_is_noop(self):
        _type("ab")
        rc._process_escape("\x1b[H")  # home
        rc._process_char("\x7f")
        assert rc._buffer == "ab"
        assert rc._cursor == 0

    def test_utf8_printable(self):
        _type("ç")
        assert rc._buffer == "ç"
        assert rc._cursor == 1


class TestControlKeys:
    def test_enter_pushes_line_and_clears(self):
        _type("run tests")
        rc._process_char("\r")
        assert rc._buffer == ""
        assert rc._cursor == 0
        assert rc.drain_queue() == ["run tests"]

    def test_enter_on_blank_does_not_queue(self):
        _type("   ")
        rc._process_char("\n")
        assert rc.drain_queue() == []

    def test_ctrl_u_clears_line(self):
        _type("garbage")
        rc._process_char("\x15")
        assert rc._buffer == ""
        assert rc._cursor == 0

    def test_ctrl_c_with_buffer_clears_not_signals(self):
        _type("abc")
        rc._process_char("\x03")  # must clear, NOT raise/SIGINT
        assert rc._buffer == ""
        assert rc._cursor == 0

    def test_ctrl_d_on_empty_queues_exit(self):
        rc._process_char("\x04")
        assert rc.drain_queue() == ["/exit"]

    def test_ctrl_d_with_buffer_is_ignored(self):
        _type("x")
        rc._process_char("\x04")
        assert rc.drain_queue() == []
        assert rc._buffer == "x"


class TestEscapeSequences:
    def test_home_and_end(self):
        _type("hello")
        rc._process_escape("\x1b[H")
        assert rc._cursor == 0
        rc._process_escape("\x1b[F")
        assert rc._cursor == 5

    def test_left_right_bounds(self):
        _type("hi")
        rc._process_escape("\x1b[C")  # right at end → no move
        assert rc._cursor == 2
        rc._process_escape("\x1b[D")
        rc._process_escape("\x1b[D")
        rc._process_escape("\x1b[D")  # left past start → clamps at 0
        assert rc._cursor == 0

    def test_delete_forward(self):
        _type("abc")
        rc._process_escape("\x1b[H")   # home
        rc._process_escape("\x1b[3~")  # delete forward
        assert rc._buffer == "bc"
        assert rc._cursor == 0

    def test_unknown_escape_is_swallowed(self):
        _type("ab")
        rc._process_escape("\x1b[Z")  # shift-tab, unhandled
        assert rc._buffer == "ab"
        assert rc._cursor == 2
