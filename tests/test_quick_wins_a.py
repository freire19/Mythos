"""Quick-wins from PROXIMO SPRINT Opcao A.

Cobre:
- #D009 — composite_tools imports promovidos para topo
- #DM015 — wizard usa yaml.safe_dump (escapa values com `:`/aspas)
- #021/#115 — .env perms 0o600 + write atomico
- #026/#076 — llm.py com httpx client compartilhado
- #D008 PERF — network_tools com aiohttp session compartilhada
- #025/#026 SEC — pytest pinned >= 9.0.2
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ─── #D009 — composite_tools imports ──────────────────────────────


class TestCompositeImportsAtTop:
    def test_executor_symbols_bound_at_module_level(self):
        """Executor imports now live at the top of _composite_helpers.py
        (post #030 split). Structural proof: the imported names are bound as
        module-level attributes — they would be missing if the import were
        inline inside _run_tool."""
        from alpha.tools import _composite_helpers

        for name in (
            "TOOL_EXECUTION_TIMEOUT",
            "_SLOW_TOOL_TIMEOUT",
            "_SLOW_TOOLS",
            "_annotate_error",
        ):
            assert hasattr(_composite_helpers, name), name

    @pytest.mark.asyncio
    async def test_run_tool_uses_top_level_annotate_error(self):
        """Behavioral proof that the top-level executor import is wired:
        _run_tool on an unknown tool routes through `_annotate_error` and
        returns the executor's `{ok: False, category}` invariant. If the
        import regressed to inline-only, this path would still work, but a
        broken/missing top-level import would surface here at call time."""
        from alpha.tools._composite_helpers import _run_tool

        result = await _run_tool("definitely_not_a_real_tool_xyz")
        assert result["ok"] is False
        assert result["category"] == "unknown_tool"


# ─── #DM015 — yaml.safe_dump no wizard ─────────────────────────────


class TestWizardYamlSafeDump:
    def test_step_create_agent_writes_parseable_yaml(self, tmp_path, monkeypatch):
        """Behavioral: an "aggressive" description with `:` and embedded quotes
        must survive a write→read round-trip. An f-string would emit broken
        YAML (`description: Use when: ...` parses as a nested dict); safe_dump
        quotes it. This fails if step_create_agent stops using safe_dump."""
        import yaml

        from alpha.wizard import steps

        nasty = "Use when: handle 'edge' \"cases\" — a: b"
        monkeypatch.setattr(steps, "_agents_dir", lambda: tmp_path)

        answers = iter(["my agent", nasty])
        monkeypatch.setattr(steps, "ask", lambda *a, **k: next(answers))
        monkeypatch.setattr(steps, "ask_yes_no", lambda *a, **k: True)

        out = steps.step_create_agent(
            {"id": "openai"}, "gpt-4o", ""
        )
        assert out is not None and out.exists()

        loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert loaded["name"] == "my-agent"
        assert loaded["description"] == nasty
        assert loaded["model"] == {"provider": "openai", "id": "gpt-4o"}

    def test_serialization_quotes_special_chars(self):
        """Description com `:` ou aspas DEVE ser escapada."""
        import yaml

        # Reproduz o payload que step_create_agent montaria para uma
        # description "agressiva" com caracteres que quebrariam f-string.
        payload = {
            "name": "test",
            "description": "Use when: handle 'edge' cases",
            "model": {"provider": "openai", "id": "gpt-4o"},
        }
        out = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        # Round-trip deve dar o mesmo dict
        assert yaml.safe_load(out) == payload


# ─── #021/#115 — .env perms + atomic write ─────────────────────────


class TestEnvWritePerms:
    # NB: the former `test_env_module_uses_atomic_write` source-grep was
    # removed — `test_write_env_creates_0o600_file` (observable perms) and
    # `test_write_env_is_atomic_no_partial_on_crash` (mocks os.replace to
    # raise and asserts no partial write + no tempfile leak) already prove
    # both the atomic-write and the 0o600 behaviors end-to-end.

    def test_env_mode_constant_is_0o600(self):
        from alpha.wizard import env

        assert env._ENV_FILE_MODE == 0o600

    def test_write_env_creates_0o600_file(self, tmp_path, monkeypatch):
        """Test funcional: write_env cria arquivo com perms 0o600."""
        from alpha.wizard import env

        env_path = tmp_path / ".env"
        monkeypatch.setattr(env, "ENV_PATH", env_path)
        monkeypatch.setattr(env, "PROJECT_ROOT", tmp_path)

        result = env.write_env({"FOO": "bar"})
        assert result == env_path
        assert env_path.exists()

        mode = env_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Esperado 0o600, got {oct(mode)}"
        assert env_path.read_text() == "FOO=bar\n"

    def test_write_env_is_atomic_no_partial_on_crash(
        self, tmp_path, monkeypatch
    ):
        """Se o write falhar no meio, o .env original nao deve ser tocado."""
        from alpha.wizard import env

        env_path = tmp_path / ".env"
        env_path.write_text("OLD_KEY=keep_me\n")
        env_path.chmod(0o600)
        monkeypatch.setattr(env, "ENV_PATH", env_path)
        monkeypatch.setattr(env, "PROJECT_ROOT", tmp_path)

        # Simula crash apos escrever no tmp mas antes do replace.
        original_replace = os.replace

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)

        with pytest.raises(OSError):
            env.write_env({"FOO": "bar"})

        # Original deve continuar intacto
        assert env_path.read_text() == "OLD_KEY=keep_me\n"

        # Nenhum tmp .env.* deve ter sobrado
        leftovers = list(tmp_path.glob(".env.*"))
        assert leftovers == [], f"tempfiles vazaram: {leftovers}"

        # Restaurar e fazer write valido
        monkeypatch.setattr(os, "replace", original_replace)
        env.write_env({"FOO": "bar"})
        assert "FOO=bar" in env_path.read_text()


# ─── #026/#076 — shared httpx LLM client ────────────────────────────


class TestSharedLLMClient:
    def test_get_shared_client_function_exists(self):
        from alpha import llm
        from alpha._http_singleton import LoopAwareClient

        assert callable(llm._get_shared_llm_client)
        # #DM042: singleton state agora encapsulado em LoopAwareClient
        # (substituiu os antigos globals _shared_llm_client/_llm_client_loop).
        assert hasattr(llm, "_shared_llm_client")
        assert isinstance(llm._shared_llm_client, LoopAwareClient)

    @pytest.mark.asyncio
    async def test_shared_client_is_reused_across_calls(self):
        """Behavioral: the LLM client is a per-loop singleton, not built
        per-call. Two `_get_shared_llm_client()` calls in the same loop must
        return the very same httpx.AsyncClient instance (the old code created
        a fresh `httpx.AsyncClient(...)` per attempt and threw it away)."""
        import httpx

        from alpha import llm

        c1 = await llm._get_shared_llm_client()
        c2 = await llm._get_shared_llm_client()
        assert isinstance(c1, httpx.AsyncClient)
        assert c1 is c2
        assert not c1.is_closed


# ─── #D008 PERF — shared aiohttp session ───────────────────────────


class TestSharedAiohttpSession:
    def test_get_shared_session_function_exists(self):
        from alpha.tools import network_tools

        assert callable(network_tools._get_shared_aiohttp_session)
        assert hasattr(network_tools, "_shared_aiohttp_session")
        assert hasattr(network_tools, "_aiohttp_session_loop")

    @pytest.mark.asyncio
    async def test_shared_session_is_reused_across_calls(self):
        """Behavioral: the aiohttp session is a per-loop singleton, not built
        per-request. Two calls in the same loop return the same, open session
        (the old code did `async with aiohttp.ClientSession()` per request)."""
        aiohttp = pytest.importorskip("aiohttp")

        from alpha.tools import network_tools

        s1 = await network_tools._get_shared_aiohttp_session()
        s2 = await network_tools._get_shared_aiohttp_session()
        assert isinstance(s1, aiohttp.ClientSession)
        assert s1 is s2
        assert not s1.closed
        await s1.close()


# ─── #025/#026 SEC — pytest pin ─────────────────────────────────────


class TestPytestPinned:
    def test_pytest_pinned_at_least_9_0(self):
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
        # Forma nao depende de tooling — apenas conferimos que NAO ha
        # mais `pytest>=8.0` (o pin antigo). >= 9.0.x ou >= 9.1 ambos
        # passam.
        assert "pytest>=8.0" not in text
        assert "pytest>=9" in text
