"""Isolamento por git worktree para sub-agentes (parity wave 2026-11, item 5).

`alpha/worktree.py` + `delegate_task(isolation="worktree")`. Testes rodam
num repo git temporário real (git está no PATH do CI). Contratos:

- `create` monta `.alpha/runs/worktrees/<id>` no branch `alpha/<id>` a
  partir de HEAD; erro claro fora de repo / sem commit;
- `changed_files` lista mudanças e ignora o scratch `.alpha/`;
- `finalize` remove worktree+branch limpos e mantém/reporta os sujos;
- `_run_subagent(isolation="worktree")` roda o sub-agente com o workspace
  apontando para o worktree, e devolve `result["worktree"]`;
- falha no prepare limpa o worktree recém-criado;
- background + isolation é rejeitado; mocks posicionais seguem válidos.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import alpha.tools.delegate_tools as dt
from alpha import worktree as wt
from alpha.config import FEATURES

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git ausente")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo), "PATH": __import__("os").environ["PATH"]},
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    (r / ".gitignore").write_text(".alpha/runs/\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture(autouse=True)
def _delegation_on(monkeypatch):
    monkeypatch.delenv("ALPHA_DELEGATED", raising=False)
    monkeypatch.setitem(FEATURES, "multi_agent_enabled", True)
    monkeypatch.setitem(FEATURES, "delegate_tool_enabled", True)


# ─── módulo worktree ─────────────────────────────────────────────────


class TestWorktreeModule:
    @pytest.mark.asyncio
    async def test_create_and_finalize_clean(self, repo):
        info = await wt.create(str(repo), "id1")
        p = Path(info["path"])
        assert p == repo / ".alpha" / "runs" / "worktrees" / "id1"
        assert (p / "a.txt").read_text() == "a\n"
        assert info["branch"] == "alpha/id1"
        assert "alpha/id1" in _git(repo, "branch", "--list", "alpha/id1")
        # main tree continua limpo (worktree está em dir ignorado)
        assert _git(repo, "status", "--porcelain").strip() == ""

        out = await wt.finalize(info)
        assert out["kept"] is False and out["changed_files"] == []
        assert not p.exists()
        assert _git(repo, "branch", "--list", "alpha/id1").strip() == ""

    @pytest.mark.asyncio
    async def test_finalize_dirty_keeps_and_reports(self, repo):
        info = await wt.create(str(repo), "id2")
        p = Path(info["path"])
        (p / "a.txt").write_text("changed\n")
        (p / "new.py").write_text("x = 1\n")
        (p / ".alpha" / "runs" / "id2").mkdir(parents=True)
        (p / ".alpha" / "runs" / "id2" / "log.txt").write_text("scratch")
        out = await wt.finalize(info)
        assert out["kept"] is True
        assert sorted(out["changed_files"]) == ["a.txt", "new.py"]
        assert "alpha/id2" in out["hint"]
        assert p.exists()
        # main checkout intocado
        assert (repo / "a.txt").read_text() == "a\n"
        assert not (repo / "new.py").exists()

    @pytest.mark.asyncio
    async def test_outside_repo_errors(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        with pytest.raises(wt.WorktreeError, match="repositório git"):
            await wt.create(str(d), "x")

    @pytest.mark.asyncio
    async def test_repo_without_commits_errors(self, tmp_path):
        r = tmp_path / "empty"
        r.mkdir()
        _git(r, "init", "-q")
        with pytest.raises(wt.WorktreeError, match="sem commits"):
            await wt.create(str(r), "x")

    @pytest.mark.asyncio
    async def test_duplicate_id_errors(self, repo):
        info = await wt.create(str(repo), "dup")
        with pytest.raises(wt.WorktreeError, match="já existe"):
            await wt.create(str(repo), "dup")
        await wt.remove(info["root"], info["path"], info["branch"], force=True)

    @pytest.mark.asyncio
    async def test_create_from_subdirectory_uses_repo_root(self, repo):
        sub = repo / "pkg"
        sub.mkdir()
        info = await wt.create(str(sub), "id3")
        assert Path(info["path"]).parent.parent.parent.parent == repo
        await wt.remove(info["root"], info["path"], info["branch"], force=True)


# ─── integração com _run_subagent ───────────────────────────────────


def _fake_run_agent(captured: dict, *, write: Path | None = None):
    async def fake(messages, user_message, **kw):
        captured.update(kw)
        captured["messages"] = messages
        if write is not None:
            target = Path(kw["workspace"]) / write
            target.write_text("edited by sub-agent\n")
        yield {"type": "done", "reply": "feito"}
    return fake


class TestRunSubagentWorktree:
    @pytest.mark.asyncio
    async def test_runs_in_worktree_and_reports_changes(self, repo, monkeypatch):
        import alpha.agent as agent_mod

        captured: dict = {}
        monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(captured, write=Path("a.txt")))
        res = await dt._run_subagent("edita a.txt", parent_workspace=str(repo), isolation="worktree")
        assert res["status"] == "completed"
        ws = Path(captured["workspace"])
        assert ws.parent == repo / ".alpha" / "runs" / "worktrees"
        assert ws.name == res["agent_id"]
        wtb = res["worktree"]
        assert wtb["kept"] is True
        assert wtb["changed_files"] == ["a.txt"]
        assert wtb["branch"] == f"alpha/{res['agent_id']}"
        assert (repo / "a.txt").read_text() == "a\n"  # main intocado
        assert (ws / "a.txt").read_text() == "edited by sub-agent\n"

    @pytest.mark.asyncio
    async def test_clean_worktree_is_removed(self, repo, monkeypatch):
        import alpha.agent as agent_mod

        captured: dict = {}
        monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(captured))
        res = await dt._run_subagent("só lê", parent_workspace=str(repo), isolation="worktree")
        assert res["worktree"]["kept"] is False
        assert not Path(captured["workspace"]).exists()
        assert _git(repo, "branch", "--list", "alpha/*").strip() == ""

    @pytest.mark.asyncio
    async def test_not_a_repo_returns_error(self, tmp_path, monkeypatch):
        d = tmp_path / "plain"
        d.mkdir()
        res = await dt._run_subagent("t", parent_workspace=str(d), isolation="worktree")
        assert res["ok"] is False and res["category"] == "worktree_error"

    @pytest.mark.asyncio
    async def test_unknown_isolation_rejected(self, repo):
        res = await dt._run_subagent("t", parent_workspace=str(repo), isolation="docker")
        assert res["ok"] is False and res["category"] == "invalid_args"

    @pytest.mark.asyncio
    async def test_prepare_failure_cleans_worktree(self, repo, monkeypatch):
        import alpha.agents as agents_mod
        from alpha.agents import AgentScope

        scope = AgentScope(name="vazio", tools_allow=["tool_inexistente"])
        monkeypatch.setattr(agents_mod, "get_agent", lambda name: scope)
        res = await dt._run_subagent(
            "t", agent="vazio", parent_workspace=str(repo), isolation="worktree",
        )
        assert res["ok"] is False
        assert not (repo / ".alpha" / "runs" / "worktrees").exists() or not any(
            (repo / ".alpha" / "runs" / "worktrees").iterdir()
        )
        assert _git(repo, "branch", "--list", "alpha/*").strip() == ""

    @pytest.mark.asyncio
    async def test_parallel_each_task_own_worktree(self, repo, monkeypatch):
        import json

        import alpha.agent as agent_mod

        seen: list[str] = []

        async def fake(messages, user_message, **kw):
            seen.append(kw["workspace"])
            (Path(kw["workspace"]) / "a.txt").write_text(user_message + "\n")
            yield {"type": "done", "reply": "ok"}

        monkeypatch.setattr(agent_mod, "run_agent", fake)
        monkeypatch.setattr(dt, "AGENT_WORKSPACE", repo)
        out = await dt._delegate_parallel(json.dumps(["t1", "t2"]), isolation="worktree")
        assert out["succeeded"] == 2
        assert len(set(seen)) == 2
        branches = [r["worktree"]["branch"] for r in out["results"]]
        assert len(set(branches)) == 2
        assert (repo / "a.txt").read_text() == "a\n"


class TestDelegateWiring:
    @pytest.mark.asyncio
    async def test_kwargs_only_when_set(self, monkeypatch):
        seen = []

        async def fake(task, context, tools_filter, provider, label="", agent="", **kw):
            seen.append(kw)
            return {"status": "completed", "result": "ok"}

        monkeypatch.setattr(dt, "_run_subagent", fake)
        await dt._delegate_task("t")
        await dt._delegate_task("t", isolation="worktree")
        await dt._delegate_task("t", isolation="worktree", inherit_context=True)
        assert seen == [{}, {"isolation": "worktree"},
                        {"inherit_context": True, "isolation": "worktree"}]

    @pytest.mark.asyncio
    async def test_background_with_isolation_rejected(self, monkeypatch):
        monkeypatch.setattr(dt, "_spawn_background_subagent", lambda *a, **k: {"ok": True})
        out = await dt._delegate_task("t", background=True, isolation="worktree")
        assert out["ok"] is False and out["category"] == "invalid_args"

    def test_schemas_expose_isolation(self):
        from alpha.tools import get_tool

        for name in ("delegate_task", "delegate_parallel"):
            props = get_tool(name).parameters["properties"]
            assert props["isolation"]["enum"] == ["", "worktree"]
