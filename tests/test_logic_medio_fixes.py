"""Regression tests for DEEP_LOGIC V1.1 MEDIOs.

Cobre #DL015 (workspace helper), #DL017 (error structure), #DL020
(loop detection role).
"""

from pathlib import Path


from alpha.executor import _annotate_error
from alpha.tools.workspace import AGENT_WORKSPACE, assert_within_workspace


# ─── #DL015 ───────────────────────────────────────────────────────


class TestAssertWithinWorkspace:
    def test_path_inside_workspace_returns_none(self):
        inside = AGENT_WORKSPACE / "subdir" / "file.txt"
        assert assert_within_workspace(inside) is None

    def test_workspace_root_itself_passes(self):
        assert assert_within_workspace(AGENT_WORKSPACE) is None

    def test_path_outside_returns_error(self):
        # /etc/passwd nunca esta dentro do workspace
        result = assert_within_workspace("/etc/passwd")
        assert result is not None
        assert "fora do workspace" in result

    def test_accepts_str_or_path(self):
        inside = str(AGENT_WORKSPACE / "x.txt")
        assert assert_within_workspace(inside) is None
        assert assert_within_workspace(Path(inside)) is None


# ─── #DL017 ───────────────────────────────────────────────────────


class TestAnnotateError:
    def test_adds_ok_false_and_category(self):
        result = _annotate_error({"error": "boom"}, "runtime")
        assert result["ok"] is False
        assert result["category"] == "runtime"
        assert result["error"] == "boom"

    def test_does_not_overwrite_existing_ok(self):
        result = _annotate_error({"ok": True, "data": 1}, "runtime")
        assert result["ok"] is True

    def test_does_not_overwrite_existing_category(self):
        result = _annotate_error(
            {"error": "boom", "category": "custom"}, "runtime"
        )
        assert result["category"] == "custom"

    def test_preserves_legacy_fields(self):
        # Backwards compat: skipped, denied, workspace_violation continuam
        # presentes pra tools que ja sabem ler eles.
        result = _annotate_error(
            {"skipped": True, "reason": "denied by user"}, "denied"
        )
        assert result["skipped"] is True
        assert result["reason"] == "denied by user"
        assert result["ok"] is False
        assert result["category"] == "denied"


# ─── #DL020 ───────────────────────────────────────────────────────


class TestLoopDetectionMessageRole:
    """Quando _detect_loop dispara, a mensagem injetada deve ser role=user
    em vez de role=system (#DL020)."""

    def test_loop_message_uses_user_role(self, monkeypatch):
        # Verificacao comportamental: roda o caminho de forced-summary com um
        # LLM stub e confirma que a mensagem injetada (com o marker) entra como
        # role=user, nao role=system (tardio confunde providers). Robusto a
        # refactors — checa o efeito, nao a proximidade no codigo-fonte.
        import asyncio
        from alpha import agent

        async def fake_stream(messages, tools, temperature, provider=None, **k):
            yield {"type": "final", "content": "ok", "usage": {}}

        monkeypatch.setattr(agent, "stream_chat_with_tools", fake_stream)

        msgs = [{"role": "system", "content": "sys"}]

        async def run():
            async for _ in agent._stream_forced_summary(
                msgs, 0.5, "deepseek", "m", "",
                "[ALPHA SYSTEM NOTE] Loop detected (x). do it",
                "loop_detected", "detail", "label",
            ):
                pass

        asyncio.run(run())
        injected = [m for m in msgs if "[ALPHA SYSTEM NOTE]" in str(m.get("content", ""))]
        assert injected, "mensagem injetada nao encontrada"
        assert injected[0]["role"] == "user", (
            "forced-summary deve injetar role=user (role=system tardia confunde providers)"
        )
        assert all(m["role"] != "system" or m is msgs[0] for m in injected), (
            "Regression: role=system reaparecu na injecao"
        )
