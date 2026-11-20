"""`/loop` — agendamento no REPL (parity wave 2026-11, item 7).

Contratos:
- `parse_interval` aceita s/m/h, aplica mín 10s e máx 24h;
- `add/pop_due/pop_one_due` vencem no tempo certo, re-ancoram em `now`
  (REPL ocupado por 3 intervalos dispara 1x, não 3x), one-shot some ao
  disparar, cap de MAX_SCHEDULES;
- `/loop` adiciona/lista/cancela com mensagens claras;
- `read_input` devolve o prompt vencido sem bloquear (inclusive no
  fallback simple-input) e, no caminho framed, o timer acorda o prompt
  só quando o buffer está vazio;
- `/loop` está no catálogo (help + autocomplete) e no dispatch.
"""

from __future__ import annotations

import pytest

from alpha import scheduler
from alpha.cli.commands import schedule_cmd


@pytest.fixture(autouse=True)
def _fresh():
    scheduler.reset_for_tests()
    yield
    scheduler.reset_for_tests()


class TestParseInterval:
    @pytest.mark.parametrize("raw,secs", [
        ("30s", 30), ("5m", 300), ("1h", 3600), ("90", 90), ("1.5h", 5400),
        (" 2 min ", 120), ("10S", 10),
    ])
    def test_ok(self, raw, secs):
        assert scheduler.parse_interval(raw) == secs

    @pytest.mark.parametrize("raw", ["", "abc", "5x", "-5m", "5s", "25h"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            scheduler.parse_interval(raw)

    def test_format(self):
        assert scheduler.format_interval(3600) == "1h"
        assert scheduler.format_interval(300) == "5m"
        assert scheduler.format_interval(45) == "45s"


class TestRegistry:
    def test_add_and_due_cycle(self):
        s = scheduler.add(60, "checa CI")
        t0 = s.next_at
        assert scheduler.pop_due(now=t0 - 1) == []
        assert scheduler.pop_due(now=t0) == [(s.id, "checa CI")]
        assert scheduler.list_schedules()[0]["fires"] == 1
        # re-ancora em now: ocupado por 3 intervalos → dispara 1x
        assert scheduler.pop_due(now=t0 + 190) == [(s.id, "checa CI")]
        assert scheduler.pop_due(now=t0 + 191) == []
        assert scheduler.list_schedules()[0]["fires"] == 2

    def test_one_shot_removed_after_fire(self):
        s = scheduler.add(60, "lembrete", max_fires=1)
        assert scheduler.pop_one_due(now=s.next_at) == (s.id, "lembrete")
        assert scheduler.list_schedules() == []
        assert scheduler.active() is False

    def test_pop_one_due_leaves_others_due(self):
        a = scheduler.add(60, "a")
        b = scheduler.add(60, "b")
        now = max(a.next_at, b.next_at)
        assert scheduler.pop_one_due(now=now) == (a.id, "a")
        assert scheduler.pop_one_due(now=now) == (b.id, "b")
        assert scheduler.pop_one_due(now=now) is None

    def test_cap_and_remove_and_clear(self):
        for i in range(scheduler.MAX_SCHEDULES):
            scheduler.add(60, f"p{i}")
        with pytest.raises(ValueError, match="máximo"):
            scheduler.add(60, "extra")
        first = scheduler.list_schedules()[0]["id"]
        assert scheduler.remove(first) is not None
        assert scheduler.remove(first) is None
        assert scheduler.clear() == scheduler.MAX_SCHEDULES - 1
        assert scheduler.next_due_in() is None

    def test_empty_prompt_rejected(self):
        with pytest.raises(ValueError):
            scheduler.add(60, "   ")

    def test_next_due_in(self):
        scheduler.add(60, "a")
        scheduler.add(600, "b")
        assert 0 < scheduler.next_due_in() <= 60


class TestLoopCommand:
    def _run(self, line: str, capsys) -> str:
        schedule_cmd._handle_loop(None, line.split())
        return capsys.readouterr().out

    def test_add_list_stop(self, capsys):
        out = self._run("/loop 5m /tasks", capsys)
        assert "Loop #1 agendado" in out and "a cada 5m" in out
        out = self._run("/loop once 20m checa o deploy", capsys)
        assert "Loop #2 agendado" in out and "uma vez" in out
        items = scheduler.list_schedules()
        assert items[0]["prompt"] == "/tasks"
        assert items[1]["prompt"] == "checa o deploy" and items[1]["max_fires"] == 1
        out = self._run("/loop list", capsys)
        assert "#1" in out and "#2" in out and "once" in out
        out = self._run("/loop stop 1", capsys)
        assert "cancelado" in out
        assert [s["id"] for s in scheduler.list_schedules()] == [2]
        out = self._run("/loop stop all", capsys)
        assert "1 loop(s)" in out
        assert self._run("/loop", capsys).strip().endswith("Nenhum loop ativo.")

    def test_errors(self, capsys):
        assert "Usage" in self._run("/loop 5m", capsys)
        assert "mínimo" in self._run("/loop 2s oi", capsys)
        assert "inválido" in self._run("/loop abc oi", capsys)
        assert "Usage" in self._run("/loop stop", capsys)
        assert "Usage" in self._run("/loop stop 99", capsys)

    def test_in_catalogue_and_dispatch(self):
        from alpha.cli.commands import _DISPATCH, iter_commands

        assert "/loop" in _DISPATCH
        assert any(name == "/loop" for name, _ in iter_commands())


class TestReadInputIntegration:
    def test_due_prompt_returned_without_blocking(self, monkeypatch, capsys):
        from alpha import repl_input

        s = scheduler.add(60, "roda os testes")
        with scheduler._lock:
            s.next_at = 0.0  # já vencido
        monkeypatch.setattr(repl_input, "use_simple_input", lambda: True)
        monkeypatch.setattr(
            repl_input, "_read_input_simple",
            lambda p: (_ for _ in ()).throw(AssertionError("não devia bloquear")),
        )
        text, imgs = repl_input.read_input("> ")
        assert text == "roda os testes" and imgs == []
        assert "loop #1" in capsys.readouterr().out
        # próximo read sem nada vencido cai no input normal
        monkeypatch.setattr(repl_input, "_read_input_simple", lambda p: ("digitado", []))
        assert repl_input.read_input("> ") == ("digitado", [])

    def test_timer_not_armed_without_schedules(self):
        from alpha import repl_input

        assert repl_input._arm_schedule_timer(object()) is None

    def test_timer_exits_only_when_buffer_empty(self, monkeypatch):
        """Simula o app do prompt_toolkit: com texto no buffer o timer re-arma;
        com buffer vazio chama app.exit(SENTINEL)."""
        import threading

        from alpha import repl_input

        scheduler.add(60, "p")
        calls: dict = {"exit": None, "timers": []}

        class _Buf:
            text = "meio digitado"

        class _Loop:
            def is_closed(self):
                return False

            def call_soon_threadsafe(self, fn):
                fn()

        class _App:
            is_running = True
            current_buffer = _Buf()
            loop = _Loop()

            def exit(self, result=None):
                calls["exit"] = result

        class _Session:
            app = _App()

        real_timer = threading.Timer

        class _FakeTimer:
            def __init__(self, delay, fn):
                self.delay, self.fn = delay, fn
                calls["timers"].append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        monkeypatch.setattr(threading, "Timer", _FakeTimer)
        try:
            handle = repl_input._arm_schedule_timer(_Session())
            assert len(calls["timers"]) == 1
            calls["timers"][0].fn()  # dispara com texto no buffer → re-arma
            assert calls["exit"] is None
            assert len(calls["timers"]) == 2
            assert calls["timers"][1].delay == repl_input._IDLE_RECHECK_S
            _Buf.text = ""
            calls["timers"][1].fn()  # buffer vazio → sai do prompt
            assert calls["exit"] == repl_input._SCHEDULE_SENTINEL
            handle.cancel()
            assert getattr(calls["timers"][1], "cancelled", False)
        finally:
            monkeypatch.setattr(threading, "Timer", real_timer)
