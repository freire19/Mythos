"""Notificações de desktop (`alpha/notify.py`).

Contratos:
- `ALPHA_NOTIFY=off` nunca notifica; `on` força; `auto` exige TTY e não-CI;
  filhos delegados/headless nunca notificam;
- `turn_finished` só notifica turnos >= `ALPHA_NOTIFY_AFTER` e zera o
  cronômetro; `approval_needed` segue o mesmo limiar; `background_finished`
  ignora o limiar;
- backend: `notify-send` (linux) / `osascript` (mac) em thread daemon, sem
  shell, texto limpo e clipado; fallback = bell no stdout;
- integração: `_run_once` marca início e fim; `approval_callback` chama o
  nudge antes do prompt; `_drain_bg_notifications` avisa.
"""

from __future__ import annotations

import threading

import pytest

from alpha import notify


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ALPHA_NOTIFY", raising=False)
    monkeypatch.delenv("ALPHA_NOTIFY_AFTER", raising=False)
    monkeypatch.delenv("ALPHA_DELEGATED", raising=False)
    monkeypatch.delenv("ALPHA_SUBAGENT_HEADLESS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    notify.reset_for_tests()
    yield
    notify.reset_for_tests()


def _capture_runs(monkeypatch):
    runs: list[list[str]] = []
    done = threading.Event()

    def fake_run(argv):
        runs.append(argv)
        done.set()

    monkeypatch.setattr(notify, "_run", fake_run)
    monkeypatch.setattr(notify, "_backend", lambda: "notify-send")
    return runs, done


class TestEnablement:
    def test_off_wins(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "off")
        monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: True, raising=False)
        assert notify.enabled() is False

    def test_on_forces_even_without_tty(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        assert notify.enabled() is True

    def test_auto_requires_tty_and_not_ci(self, monkeypatch):
        monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: False, raising=False)
        assert notify.enabled() is False
        monkeypatch.setattr(notify.sys.stdout, "isatty", lambda: True, raising=False)
        assert notify.enabled() is True
        monkeypatch.setenv("CI", "1")
        assert notify.enabled() is False

    @pytest.mark.parametrize("var", ["ALPHA_DELEGATED", "ALPHA_SUBAGENT_HEADLESS"])
    def test_children_never_notify(self, monkeypatch, var):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        monkeypatch.setenv(var, "1")
        assert notify.enabled() is False

    def test_invalid_mode_falls_back_to_auto(self, monkeypatch):
        from alpha.config import notify_mode, notify_after_seconds
        monkeypatch.setenv("ALPHA_NOTIFY", "loud")
        assert notify_mode() == "auto"
        monkeypatch.setenv("ALPHA_NOTIFY_AFTER", "-5")
        assert notify_after_seconds() == 0
        monkeypatch.setenv("ALPHA_NOTIFY_AFTER", "abc")
        assert notify_after_seconds() == 20


class TestThreshold:
    def test_short_turn_is_silent(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        runs, _ = _capture_runs(monkeypatch)
        notify.turn_started()
        assert notify.turn_finished("ok") is False
        assert runs == []

    def test_long_turn_notifies_and_resets(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        runs, done = _capture_runs(monkeypatch)
        notify.turn_started()
        monkeypatch.setattr(notify, "turn_elapsed", lambda: 42.0)
        assert notify.turn_finished("Terminei\x1b[31m a\n\n tarefa " + "x" * 500) is True
        assert done.wait(2)
        argv = runs[0]
        assert argv[0] == "notify-send" and "Pronto após 42s" in argv
        body = argv[-1]
        assert body.startswith("Terminei") and "\x1b" not in body and "\n" not in body
        assert len(body) <= notify._BODY_CAP
        # Cronômetro zerado: sem novo turn_started, o próximo não dispara.
        monkeypatch.setattr(notify, "turn_elapsed", lambda: 0.0)
        assert notify.turn_finished("x") is False

    def test_threshold_zero_means_every_turn(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        monkeypatch.setenv("ALPHA_NOTIFY_AFTER", "0")
        runs, done = _capture_runs(monkeypatch)
        notify.turn_started()
        assert notify.turn_finished("") is True
        assert done.wait(2) and runs[0][-1] == "Turno concluído."

    def test_approval_follows_threshold(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        runs, done = _capture_runs(monkeypatch)
        notify.turn_started()
        assert notify.approval_needed("execute_shell") is False
        monkeypatch.setattr(notify, "turn_elapsed", lambda: 99.0)
        assert notify.approval_needed("execute_shell") is True
        assert done.wait(2) and "execute_shell precisa da sua confirmação." in runs[0]

    def test_background_ignores_threshold(self, monkeypatch):
        monkeypatch.setenv("ALPHA_NOTIFY", "on")
        runs, done = _capture_runs(monkeypatch)
        assert notify.background_finished("task #3") is True
        assert done.wait(2) and runs[0][-1] == "task #3"


class TestBackends:
    def test_bell_fallback_writes_to_stdout(self, monkeypatch, capsys):
        monkeypatch.setattr(notify, "_backend", lambda: "bell")
        assert notify.send("t", "b") is True
        assert capsys.readouterr().out == "\a"

    def test_osascript_escapes_quotes(self, monkeypatch):
        runs, done = _capture_runs(monkeypatch)
        monkeypatch.setattr(notify, "_backend", lambda: "osascript")
        notify.send("Pronto", 'disse "oi" \\ fim')
        assert done.wait(2)
        assert runs[0][:2] == ["osascript", "-e"]
        assert '\\"oi\\"' in runs[0][2] and "\\\\" in runs[0][2]

    def test_backend_detection(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "linux")
        monkeypatch.setattr(notify.shutil, "which", lambda n: "/usr/bin/notify-send" if n == "notify-send" else None)
        assert notify._backend() == "notify-send"
        monkeypatch.setattr(notify.shutil, "which", lambda n: None)
        assert notify._backend() == "bell"
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(notify.shutil, "which", lambda n: "/usr/bin/osascript" if n == "osascript" else None)
        assert notify._backend() == "osascript"

    def test_run_never_raises(self, monkeypatch):
        import subprocess

        def boom(*a, **k):
            raise OSError("no exec")

        monkeypatch.setattr(subprocess, "run", boom)
        notify._run(["definitely-not-a-binary"])  # no exception


class TestIntegration:
    def test_approval_callback_nudges_before_prompt(self, monkeypatch):
        from alpha.cli import setup as setup_mod

        order: list[str] = []
        monkeypatch.setattr(notify, "approval_needed", lambda name: order.append(f"nudge:{name}"))
        monkeypatch.setattr(
            setup_mod, "print_approval_request",
            lambda name, args: (order.append("prompt"), False)[1],
        )
        assert setup_mod.approval_callback("execute_shell", {"command": "ls"}) is False
        assert order == ["nudge:execute_shell", "prompt"]

    @pytest.mark.asyncio
    async def test_bg_finish_calls_notify(self, monkeypatch, tmp_path):
        import alpha.agent as agent_mod
        from alpha import bg_tasks

        monkeypatch.setattr(bg_tasks, "_bg_dir", lambda: tmp_path)
        bg_tasks.reset_for_tests()
        got: list[str] = []
        monkeypatch.setattr(notify, "background_finished", lambda d: got.append(d))
        r = bg_tasks.start("true", cwd=str(tmp_path))
        import time
        for _ in range(100):
            if not bg_tasks.get(r["task_id"]).running:
                break
            time.sleep(0.02)
        messages = [{"role": "user", "content": "oi"}]
        assert agent_mod._drain_bg_notifications(messages) is not None
        assert got == [f"task #{r['task_id']}"]

    def test_run_once_marks_turn(self):
        import inspect

        import alpha.cli.entry as entry
        assert "_notify.turn_started()" in inspect.getsource(entry._run_once)
        # O fim do turno (append do reply) vive no caller do _run_once.
        assert "_notify.turn_finished(reply or \"\")" in inspect.getsource(entry)
