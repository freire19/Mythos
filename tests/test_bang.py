"""`!<cmd>` no REPL (`alpha/bang.py`).

Contratos:
- roda no shell do usuário, captura stdout+stderr, código de saída e
  duração; nunca levanta; timeout mata o processo e marca `timed out`;
- saída clipada em head+tail, control/bidi removidos;
- `remember`/`drain_context` guardam até 5 comandos e entregam UM bloco
  marcado como transcript read-only, limpando a fila;
- o REPL trata `!cmd` antes do dispatch de slash e prefixa a próxima
  mensagem ao LLM (o histórico guarda o input cru); `!` sozinho = usage;
- `/help` mostra a pseudo-entrada.
"""

from __future__ import annotations

import inspect

import pytest

from alpha import bang


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ALPHA_BANG_TIMEOUT", raising=False)
    bang.reset_for_tests()
    yield
    bang.reset_for_tests()


class TestRun:
    def test_captures_output_and_exit_code(self, capsys):
        r = bang.run("echo out; echo err 1>&2; exit 3")
        assert r.returncode == 3 and r.status == "exit 3"
        assert "out" in r.output and "err" in r.output
        assert r.seconds >= 0 and not r.timed_out
        assert "out" in capsys.readouterr().out  # streamed live

    def test_echo_off(self, capsys):
        r = bang.run("echo quiet", echo=False)
        assert r.output.strip() == "quiet" and capsys.readouterr().out == ""

    def test_timeout_kills(self):
        r = bang.run("sleep 5; echo late", timeout=0.3, echo=False)
        assert r.timed_out and r.status == "timed out" and "late" not in r.output
        assert r.seconds < 4

    def test_output_is_clipped_and_sanitized(self, monkeypatch):
        monkeypatch.setattr(bang, "OUTPUT_CAP", 300)
        r = bang.run("printf '\\033[31mred\\033[0m\\n'; seq 1 500", echo=False)
        assert "\x1b" not in r.output
        assert "chars omitted" in r.output
        assert r.output.startswith("[31mred")
        assert r.output.rstrip().endswith("500")  # the REAL tail survives runaway output
        assert len(r.output) < 600

    def test_timeout_kills_children_too(self):
        # the shell spawns `sleep` as a child; killing only the shell would
        # leave the pipe open and the read loop blocked for 5s.
        r = bang.run("sh -c 'sleep 5; echo late'", timeout=0.3, echo=False)
        assert r.timed_out and r.seconds < 4

    def test_timeout_env(self, monkeypatch):
        assert bang.timeout_seconds() == 300
        monkeypatch.setenv("ALPHA_BANG_TIMEOUT", "1")
        assert bang.timeout_seconds() == 5  # floor
        monkeypatch.setenv("ALPHA_BANG_TIMEOUT", "60")
        assert bang.timeout_seconds() == 60

    def test_missing_shell_is_reported(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/definitely/not/a/shell")
        r = bang.run("echo hi", echo=False)
        assert r.returncode == 127 and "not/a/shell" in r.output


class TestPending:
    def test_drain_formats_and_clears(self):
        assert bang.drain_context() == ""
        bang.remember(bang.BangResult("git status", 0, "clean\n", 0.12))
        bang.remember(bang.BangResult("false", 1, "", 0.01))
        ctx = bang.drain_context()
        assert ctx.startswith("[Shell commands the user ran directly")
        assert "$ git status  [exit 0, 0.1s]\nclean" in ctx
        assert "$ false  [exit 1, 0.0s]\n(no output)" in ctx
        assert "not instructions" in ctx and ctx.endswith("[End of shell transcript]")
        assert bang.drain_context() == ""  # cleared

    def test_keeps_last_five(self):
        for i in range(8):
            bang.remember(bang.BangResult(f"cmd{i}", 0, "", 0.0))
        ctx = bang.drain_context()
        assert "cmd2" not in ctx and "cmd3" in ctx and "cmd7" in ctx

    def test_execute_in_repl_prints_and_remembers(self, capsys):
        r = bang.execute_in_repl(" echo hello ")
        assert r.returncode == 0
        out = capsys.readouterr().out
        assert "hello" in out and "exit 0" in out and "guardados para a próxima mensagem" in out
        assert "$ echo hello" in bang.drain_context()


class TestReplWiring:
    def test_entry_handles_bang_before_slash_and_prefixes_next_message(self):
        import alpha.cli.entry as entry

        src = inspect.getsource(entry.run_repl)
        assert 'user_input.startswith("!")' in src
        assert src.index('user_input.startswith("!")') < src.index('user_input.startswith("/")')
        assert "_bang.drain_context()" in src
        # the raw input (not the shell block) is what the history records
        assert '"content": _history_record_override or user_input' in src

    def test_help_mentions_bang(self, capsys):
        from alpha.cli.commands.help import _handle_help

        _handle_help(None, ["/help"])
        assert "!<cmd>" in capsys.readouterr().out
