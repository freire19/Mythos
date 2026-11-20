"""DEEP_LOGIC wave 2 — entry.py × checkpoints.

#DL210: run_print emite marcador de turno (fronteira do /rewind) — edições
        de uma invocação headless não são varridas como "o último turno"
        por um REPL futuro no mesmo workspace.
#DL218: ALPHA_AUTO_CONTINUE_MAX roteado por _env_int_clamped (clamp mínimo
        1 + fallback no default), não mais int() cru.
"""

import pytest

import alpha.cli.entry as entry
from alpha import checkpoints


# ─── #DL218 — cap de auto-continue clampado ───


class TestAutoContinueLimit:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("ALPHA_AUTO_CONTINUE_MAX", raising=False)
        assert entry._auto_continue_limit() == entry._AUTO_CONTINUE_MAX_DEFAULT

    def test_valid_value_passes_through(self, monkeypatch):
        monkeypatch.setenv("ALPHA_AUTO_CONTINUE_MAX", "5")
        assert entry._auto_continue_limit() == 5

    def test_zero_clamps_to_one(self, monkeypatch):
        monkeypatch.setenv("ALPHA_AUTO_CONTINUE_MAX", "0")
        assert entry._auto_continue_limit() == 1

    def test_negative_clamps_to_one(self, monkeypatch):
        monkeypatch.setenv("ALPHA_AUTO_CONTINUE_MAX", "-3")
        assert entry._auto_continue_limit() == 1

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ALPHA_AUTO_CONTINUE_MAX", "abc")
        assert entry._auto_continue_limit() == entry._AUTO_CONTINUE_MAX_DEFAULT

    def test_empty_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ALPHA_AUTO_CONTINUE_MAX", "")
        assert entry._auto_continue_limit() == entry._AUTO_CONTINUE_MAX_DEFAULT


# ─── #DL210 — marcador de turno no modo print ───


@pytest.fixture()
def _ws(tmp_path, monkeypatch):
    """Workspace isolado por teste + checkpoints ligados."""
    import alpha.tools.path_helpers as ph_mod
    import alpha.tools.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ph_mod, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.delenv("ALPHA_CHECKPOINTS", raising=False)
    monkeypatch.delenv("ALPHA_CHECKPOINT_LIMIT", raising=False)
    yield tmp_path


class TestRunPrintMarksTurn:
    def _run(self, monkeypatch, calls):
        import alpha.sdk as sdk_mod

        monkeypatch.setattr(
            entry, "_setup_agent_session",
            lambda provider, temperature: (
                None, provider, temperature, "sys", lambda name: None, [],
            ),
        )
        monkeypatch.setattr(
            sdk_mod, "run_task_sync",
            lambda *a, **kw: sdk_mod.TaskResult(reply="ok", turns=1),
        )
        monkeypatch.setattr(
            checkpoints, "mark_turn", lambda label: calls.append(label),
        )
        with pytest.raises(SystemExit) as exc:
            entry.run_print("deepseek", 0.5, "faça algo")
        return exc.value.code

    def test_marker_emitted_before_task(self, monkeypatch):
        calls: list[str] = []
        code = self._run(monkeypatch, calls)
        assert code == 0
        assert calls == ["faça algo"]


class TestPrintEditsNotSweptByReplRewind:
    def test_repl_rewind_stops_at_print_marker(self, _ws):
        """Edições do -p (com marcador) não entram no /rewind do turno REPL."""
        a, b = _ws / "print.txt", _ws / "repl.txt"
        a.write_text("a1")
        b.write_text("b1")

        # invocação headless: marcador + edição
        checkpoints.mark_turn("tarefa headless")
        checkpoints.snapshot(a, "write_file")
        a.write_text("a2")

        # turno REPL subsequente no mesmo workspace
        checkpoints.mark_turn("turno repl")
        checkpoints.snapshot(b, "edit_file")
        b.write_text("b2")

        result = checkpoints.rewind()
        assert b.read_text() == "b1"
        assert a.read_text() == "a2"
        assert len(result["restored"]) == 1

    def test_second_rewind_reaches_print_turn(self, _ws):
        a = _ws / "print.txt"
        a.write_text("a1")
        checkpoints.mark_turn("tarefa headless")
        checkpoints.snapshot(a, "write_file")
        a.write_text("a2")

        checkpoints.mark_turn("turno repl")
        b = _ws / "repl.txt"
        checkpoints.snapshot(b, "write_file")
        b.write_text("b1")

        checkpoints.rewind()
        checkpoints.rewind()
        assert a.read_text() == "a1"
