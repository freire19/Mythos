"""DEEP_LOGIC wave 2 — bg_tasks (#DL209, #DL216).

#DL209: log de bg namespaceado por pid + open sem truncar — dois processos
Alpha no mesmo workspace não podem clobberar o log um do outro.
#DL216: cap de 50 é de CONCORRÊNCIA (active), não de histórico — tasks
terminadas não bloqueiam novas.
"""

from __future__ import annotations

import os

import pytest

from alpha import bg_tasks


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "alpha.tools.workspace.AGENT_WORKSPACE", str(tmp_path), raising=False
    )
    bg_tasks.reset_for_tests()
    yield
    for t in list(bg_tasks._tasks.values()):
        try:
            t.proc.kill()
        except OSError:
            pass
    bg_tasks.reset_for_tests()


def _wait_finished(task_id: int, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = bg_tasks.get(task_id)
        if task is not None and not task.running:
            return
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} não terminou em {timeout}s")


# ---------------------------------------------------------------------------
# #DL209 — log namespaceado por pid, aberto sem truncar
# ---------------------------------------------------------------------------


def test_dl209_log_path_inclui_pid(tmp_path):
    res = bg_tasks.start("true", cwd=str(tmp_path))
    assert "error" not in res
    assert f"task-{os.getpid()}-{res['task_id']}.log" in res["log"]


def test_dl209_start_nao_trunca_log_preexistente(tmp_path):
    # Simula o filho detached de OUTRO processo Alpha que já escreveu no
    # mesmo nome de arquivo (colisão de id): o start não pode truncar.
    bg_dir = tmp_path / ".alpha" / "runs" / "bg"
    bg_dir.mkdir(parents=True)
    preexisting = bg_dir / f"task-{os.getpid()}-1.log"
    preexisting.write_bytes(b"saida-do-outro-processo\n")

    res = bg_tasks.start("echo novo", cwd=str(tmp_path))
    assert res["task_id"] == 1
    _wait_finished(1)

    data = preexisting.read_bytes()
    assert b"saida-do-outro-processo" in data
    assert b"novo" in data


def test_dl209_output_resolve_log_correto(tmp_path):
    res = bg_tasks.start("echo marcador-dl209", cwd=str(tmp_path))
    task_id = res["task_id"]
    _wait_finished(task_id)

    out = bg_tasks.output(task_id)
    assert "marcador-dl209" in out.get("output", "")
    listed = bg_tasks.list_tasks()
    assert any(t["task_id"] == task_id and f"task-{os.getpid()}-" in t["log"] for t in listed)


# ---------------------------------------------------------------------------
# #DL216 — cap por concorrência, não por histórico
# ---------------------------------------------------------------------------


def test_dl216_tasks_terminadas_nao_bloqueiam_novas(tmp_path, monkeypatch):
    monkeypatch.setattr(bg_tasks, "_MAX_TASKS", 3)
    for _ in range(3):
        res = bg_tasks.start("true", cwd=str(tmp_path))
        assert "error" not in res
        _wait_finished(res["task_id"])

    # Registro tem 3 entradas (== cap), todas terminadas → deve aceitar.
    res = bg_tasks.start("true", cwd=str(tmp_path))
    assert "error" not in res
    _wait_finished(res["task_id"])


def test_dl216_cap_bloqueia_apenas_ativas(tmp_path, monkeypatch):
    monkeypatch.setattr(bg_tasks, "_MAX_TASKS", 2)
    running = [bg_tasks.start("sleep 30", cwd=str(tmp_path)) for _ in range(2)]
    assert all("error" not in r for r in running)

    res = bg_tasks.start("true", cwd=str(tmp_path))
    assert "error" in res
    assert "ativas" in res["error"]

    bg_tasks.kill(running[0]["task_id"])
    res = bg_tasks.start("true", cwd=str(tmp_path))
    assert "error" not in res
    bg_tasks.kill(running[1]["task_id"])


def test_dl216_registro_evicta_terminadas_notificadas(tmp_path, monkeypatch):
    monkeypatch.setattr(bg_tasks, "_MAX_TASKS", 1)
    # Enche o histórico além de _MAX_TASKS * 4 com tasks terminadas+notificadas.
    for _ in range(5):
        res = bg_tasks.start("true", cwd=str(tmp_path))
        assert "error" not in res
        _wait_finished(res["task_id"])
        bg_tasks.drain_finished_unnotified()

    before = len(bg_tasks._tasks)
    assert before == 5
    res = bg_tasks.start("true", cwd=str(tmp_path))
    assert "error" not in res
    assert len(bg_tasks._tasks) < before + 1
    _wait_finished(res["task_id"])
