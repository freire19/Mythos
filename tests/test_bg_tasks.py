"""Background task manager (alpha/bg_tasks.py) + integração execute_shell.

start() registra processo detached com log; list/output/kill dão controle;
drain_finished_unnotified() alimenta a notificação do loop do agente.
"""

import time

import pytest

from alpha import bg_tasks


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import alpha.tools.path_helpers as ph_mod
    import alpha.tools.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.setattr(ph_mod, "AGENT_WORKSPACE", tmp_path)
    bg_tasks.reset_for_tests()
    yield tmp_path
    # Não deixar processos órfãos de teste rodando.
    for t in bg_tasks.list_tasks():
        if t["status"] == "running":
            bg_tasks.kill(t["task_id"])
    bg_tasks.reset_for_tests()


def _wait_finished(task_id: int, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = next(
            (t for t in bg_tasks.list_tasks() if t["task_id"] == task_id), None
        )
        if info and info["status"] == "finished":
            return info
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} não terminou em {timeout}s")


class TestBgTasks:
    def test_start_and_finish_with_log(self, _isolated):
        r = bg_tasks.start("echo hello-bg", cwd=str(_isolated))
        assert "error" not in r
        info = _wait_finished(r["task_id"])
        assert info["exit_code"] == 0

        out = bg_tasks.output(r["task_id"])
        assert "hello-bg" in out["output"]

    def test_nonzero_exit_captured(self, _isolated):
        r = bg_tasks.start("ls /caminho/que/nao/existe-xyz", cwd=str(_isolated))
        info = _wait_finished(r["task_id"])
        assert info["exit_code"] not in (0, None)

    def test_kill_running_task(self, _isolated):
        r = bg_tasks.start("sleep 30", cwd=str(_isolated))
        assert "error" not in r
        result = bg_tasks.kill(r["task_id"])
        assert result["status"] == "killed"
        info = next(t for t in bg_tasks.list_tasks() if t["task_id"] == r["task_id"])
        assert info["status"] == "finished"

    def test_drain_notifies_exactly_once(self, _isolated):
        r = bg_tasks.start("echo done", cwd=str(_isolated))
        _wait_finished(r["task_id"])

        first = bg_tasks.drain_finished_unnotified()
        assert [t["task_id"] for t in first] == [r["task_id"]]
        assert bg_tasks.drain_finished_unnotified() == []

        note = bg_tasks.format_notification(first)
        assert "[ALPHA SYSTEM NOTE]" in note
        assert f"Task #{r['task_id']}" in note

    def test_pipes_rejected(self, _isolated):
        r = bg_tasks.start("echo a | grep a", cwd=str(_isolated))
        assert "error" in r

    def test_unknown_task_output_and_kill(self, _isolated):
        assert "error" in bg_tasks.output(999)
        assert "error" in bg_tasks.kill(999)


class TestExecuteShellBackground:
    @pytest.mark.asyncio
    async def test_background_returns_task_id(self, _isolated):
        from alpha.tools.shell_tools import _execute_shell

        r = await _execute_shell("echo via-tool", background=True)
        assert "error" not in r, r
        assert "task_id" in r
        info = _wait_finished(r["task_id"])
        assert info["exit_code"] == 0
        out = bg_tasks.output(r["task_id"])
        assert "via-tool" in out["output"]

    @pytest.mark.asyncio
    async def test_background_fail_closed_when_sandbox_unavailable(
        self, _isolated, monkeypatch
    ):
        # #100: background must NOT bypass the sandbox. When sandbox is enabled
        # but no backend is installed, wrap_command raises → fail-closed (block),
        # never a raw Popen outside the jail.
        from alpha import shell_sandbox as sandbox
        from alpha.tools.shell_tools import _execute_shell

        def _no_backend(cmd_parts, **kw):
            raise sandbox.SandboxUnavailableError("no backend")

        monkeypatch.setattr(sandbox, "wrap_command", _no_backend)
        r = await _execute_shell("echo x", background=True)
        assert r.get("blocked") is True
        assert "task_id" not in r

    @pytest.mark.asyncio
    async def test_background_still_validates_command(self, _isolated):
        from alpha.tools.shell_tools import _execute_shell

        r = await _execute_shell("rm -rf /", background=True)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_foreground_unchanged(self, _isolated):
        from alpha.tools.shell_tools import _execute_shell

        r = await _execute_shell("echo foreground")
        assert r.get("exit_code") == 0
        assert "foreground" in r.get("stdout", "")


class TestAgentNotification:
    @pytest.mark.asyncio
    async def test_finished_task_injected_into_turn(self, _isolated, monkeypatch):
        import alpha.agent as agent_mod

        r = bg_tasks.start("echo notifica", cwd=str(_isolated))
        _wait_finished(r["task_id"])

        async def fake_stream(messages, tools, temperature, **_kw):
            yield {"type": "final", "content": "ok", "tool_calls": [], "error": None}

        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        messages = [{"role": "user", "content": "oi"}]
        events = []
        async for event in agent_mod.run_agent(
            messages=messages, user_message="oi", provider="deepseek",
        ):
            events.append(event)

        notes = [
            m for m in messages
            if m.get("role") == "user"
            and "Background task(s) finished" in str(m.get("content", ""))
        ]
        assert len(notes) == 1
        assert any(
            e.get("type") == "anomaly" and e.get("reason") == "bg_task_finished"
            for e in events
        )
        # Notificado uma vez só: outra rodada não re-injeta.
        messages2 = [{"role": "user", "content": "de novo"}]
        async for _ in agent_mod.run_agent(
            messages=messages2, user_message="de novo", provider="deepseek",
        ):
            pass
        assert not any(
            "Background task(s) finished" in str(m.get("content", ""))
            for m in messages2
        )

    @pytest.mark.asyncio
    async def test_subagent_does_not_receive_notifications(self, _isolated, monkeypatch):
        import alpha.agent as agent_mod

        r = bg_tasks.start("echo sub", cwd=str(_isolated))
        _wait_finished(r["task_id"])

        async def fake_stream(messages, tools, temperature, **_kw):
            yield {"type": "final", "content": "ok", "tool_calls": [], "error": None}

        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        messages = [{"role": "user", "content": "tarefa"}]
        async for _ in agent_mod.run_agent(
            messages=messages, user_message="tarefa", provider="deepseek",
            bg_notifications=False,
        ):
            pass
        assert not any(
            "Background task(s) finished" in str(m.get("content", ""))
            for m in messages
        )


class TestCapCountsOnlyActive:
    """AUDIT_V2.3 #307: o cap limita tasks ATIVAS — terminadas (nunca
    recolhidas) não podem esgotar o limite da sessão."""

    def _corpse(self, i: int, tmp_path) -> "bg_tasks.BgTask":
        import subprocess as sp

        proc = sp.Popen(["true"])
        proc.wait()
        t = bg_tasks.BgTask(
            id=i, command="true", cwd=".", proc=proc,
            log_path=tmp_path / f"corpse-{i}.log",
        )
        t.exit_code = 0
        t.notified = True
        return t

    def test_finished_tasks_dont_count_toward_cap(self, _isolated, tmp_path):
        with bg_tasks._lock:
            for i in range(1000, 1000 + bg_tasks._MAX_TASKS):
                bg_tasks._tasks[i] = self._corpse(i, tmp_path)
        out = bg_tasks.start("echo oi", str(tmp_path))
        assert "error" not in out, out
        bg_tasks.kill(out["task_id"])

    def test_old_finished_notified_are_pruned(self, _isolated, tmp_path):
        n = bg_tasks._MAX_TASKS * 4 + 1
        with bg_tasks._lock:
            for i in range(2000, 2000 + n):
                bg_tasks._tasks[i] = self._corpse(i, tmp_path)
        before = len(bg_tasks._tasks)
        out = bg_tasks.start("echo oi", str(tmp_path))
        assert "error" not in out, out
        bg_tasks.kill(out["task_id"])
        # poda removeu um lote de terminadas+notificadas
        assert len(bg_tasks._tasks) < before
