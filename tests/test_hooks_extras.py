"""Testes dos eventos novos de hook (session_start, pre_compact,
subagent_stop) e do strip de secrets no env dos hooks (#D118).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha import hooks


@pytest.fixture(autouse=True)
def _reset_hook_cache():
    hooks.reset_cache()
    yield
    hooks.reset_cache()


@pytest.fixture
def settings_in_cwd(tmp_path: Path, monkeypatch):
    # These tests exercise hook EXECUTION, so opt in to trusting project-scoped
    # config (#D301 makes project hooks fail-closed by default — the gate itself
    # is covered by test_project_config_trust.py).
    monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
    monkeypatch.chdir(tmp_path)
    alpha_dir = tmp_path / ".alpha"
    alpha_dir.mkdir()
    return alpha_dir / "settings.json"


def _write_settings(path: Path, hooks_block: dict) -> None:
    path.write_text(json.dumps({"hooks": hooks_block}), encoding="utf-8")


# ── Eventos novos registráveis ───────────────────────────────────


class TestNewEvents:
    def test_valid_events_extended(self):
        for ev in ("session_start", "pre_compact", "subagent_stop"):
            assert ev in hooks.VALID_EVENTS

    def test_session_start_hook_fires(self, settings_in_cwd: Path, tmp_path: Path):
        out = tmp_path / "out.txt"
        _write_settings(settings_in_cwd, {"session_start": [
            {"command": f"cat > {out}"},
        ]})
        hooks.fire("session_start", extra={"provider": "deepseek"})
        payload = json.loads(out.read_text())
        assert payload["event"] == "session_start"
        assert payload["provider"] == "deepseek"

    def test_pre_compact_hook_receives_metrics(self, settings_in_cwd: Path, tmp_path: Path):
        out = tmp_path / "out.txt"
        _write_settings(settings_in_cwd, {"pre_compact": [
            {"command": f"cat > {out}"},
        ]})
        hooks.fire("pre_compact", extra={"tokens_before": 5000, "messages_count": 42})
        payload = json.loads(out.read_text())
        assert payload["tokens_before"] == 5000
        assert payload["messages_count"] == 42

    def test_subagent_stop_hook_fires(self, settings_in_cwd: Path, tmp_path: Path):
        out = tmp_path / "out.txt"
        _write_settings(settings_in_cwd, {"subagent_stop": [
            {"command": f"cat > {out}"},
        ]})
        hooks.fire(
            "subagent_stop",
            extra={"agent_id": "abc", "status": "completed", "tools_used": 3},
        )
        payload = json.loads(out.read_text())
        assert payload["agent_id"] == "abc"
        assert payload["status"] == "completed"


# ── Strip de secrets (#D118) ─────────────────────────────────────


class TestSecretStripping:
    def test_secret_vars_stripped_by_default(
        self, settings_in_cwd: Path, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-vazou")
        monkeypatch.setenv("MY_GITHUB_TOKEN", "ghp-vazou")
        monkeypatch.setenv("DB_PASSWORD", "senha-vazou")
        monkeypatch.setenv("MY_PROJECT_DIR", "/projetos/x")
        out = tmp_path / "env.txt"
        _write_settings(settings_in_cwd, {"on_stop": [
            {"command": f'echo "[$OPENAI_API_KEY][$MY_GITHUB_TOKEN][$DB_PASSWORD][$MY_PROJECT_DIR]" > {out}'},
        ]})
        hooks.fire("on_stop")
        assert out.read_text().strip() == "[][][][/projetos/x]"

    def test_pass_secrets_opt_in(
        self, settings_in_cwd: Path, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-liberado")
        out = tmp_path / "env.txt"
        _write_settings(settings_in_cwd, {"on_stop": [
            {"command": f'echo "[$OPENAI_API_KEY]" > {out}', "pass_secrets": True},
        ]})
        hooks.fire("on_stop")
        assert out.read_text().strip() == "[sk-liberado]"

    def test_alpha_vars_still_injected(
        self, settings_in_cwd: Path, tmp_path: Path
    ):
        out = tmp_path / "env.txt"
        _write_settings(settings_in_cwd, {"on_user_prompt": [
            {"command": f'echo "$ALPHA_HOOK_EVENT/$ALPHA_USER_PROMPT" > {out}'},
        ]})
        hooks.fire("on_user_prompt", user_prompt="oi")
        assert out.read_text().strip() == "on_user_prompt/oi"

    def test_hook_env_helper(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setenv("SAFE_VAR", "y")
        env = hooks._hook_env(pass_secrets=False)
        assert "ANTHROPIC_API_KEY" not in env
        assert env["SAFE_VAR"] == "y"
        env_full = hooks._hook_env(pass_secrets=True)
        assert env_full["ANTHROPIC_API_KEY"] == "x"


# ── Fire sites ───────────────────────────────────────────────────


class TestFireSites:
    @pytest.mark.anyio
    async def test_subagent_stop_fired_on_completion(self, monkeypatch, tmp_path):
        import alpha.agent as agent_mod
        from alpha.tools import load_all_tools
        from alpha.tools.delegate_tools import _run_subagent

        load_all_tools()
        fired: list[dict] = []
        monkeypatch.setattr(
            hooks, "fire",
            lambda event, **kw: fired.append({"event": event, **kw.get("extra", {})}),
        )
        monkeypatch.setattr(hooks, "has_event", lambda ev: ev == "subagent_stop")

        async def fake_run_agent(**kwargs):
            yield {"type": "done", "reply": "ok"}

        monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)

        result = await _run_subagent("tarefa", parent_workspace=str(tmp_path))
        assert result["status"] == "completed"
        assert len(fired) == 1
        assert fired[0]["event"] == "subagent_stop"
        assert fired[0]["status"] == "completed"
        assert fired[0]["agent_id"] == result["agent_id"]

    def test_session_start_fired_in_setup(self, monkeypatch):
        import alpha.cli.entry as alpha_main

        fired: list[str] = []
        monkeypatch.setattr(
            alpha_main.hooks, "fire",
            lambda event, **kw: fired.append(event),
        )
        monkeypatch.setattr(alpha_main, "_resolve_active_agent", lambda: None)
        monkeypatch.setattr(
            alpha_main, "_apply_agent_overrides", lambda a, p, t: (p, t)
        )
        monkeypatch.setattr(alpha_main, "_build_system_prompt", lambda a: "sys")
        monkeypatch.setattr(
            alpha_main, "_get_tools_for_agent", lambda a: (lambda n: None, [])
        )

        alpha_main._setup_agent_session("deepseek", 0.5)
        assert fired == ["session_start"]


class TestHookTimeoutKillsProcessGroup:
    """AUDIT_V2.3 #306: no timeout, o grupo de processos inteiro morre —
    não só o /bin/sh imediato (netos ficavam órfãos)."""

    def test_grandchild_killed_on_timeout(self, monkeypatch):
        import subprocess
        import time
        import uuid

        from alpha import hooks as hooks_mod

        token = f"alpha306{uuid.uuid4().hex[:12]}"
        monkeypatch.setattr(hooks_mod, "HOOK_TIMEOUT", 1)
        # O neto (sh interno com $0 = token, visível no pgrep -f) dorme em
        # background; o filho imediato também dorme → timeout de 1s.
        hook = hooks_mod.Hook(
            event="post_tool",
            command=f"sh -c 'sleep 300' '{token}' & sleep 300",
        )
        code, _out, err = hooks_mod._run_hook(hook, {"x": 1}, {})
        assert code == 124
        assert "timed out" in err
        time.sleep(0.2)
        r = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True)
        assert r.returncode != 0, f"neto órfão sobreviveu ao timeout: {r.stdout}"

    def test_fast_hook_unaffected(self, monkeypatch):
        from alpha import hooks as hooks_mod

        hook = hooks_mod.Hook(event="post_tool", command="echo ok")
        code, out, err = hooks_mod._run_hook(hook, {"x": 1}, {})
        assert code == 0
        assert "ok" in out
