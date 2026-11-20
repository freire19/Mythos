"""Regression tests for DEEP_PERFORMANCE V1.0 stragglers.

Cobre:
- #D006 — git regex pre-compilada module-level
- #D007 — list_sessions partial-read otimizacao
- #D009 — DDGS instancia compartilhada
- #D010 — composite rglob unificado
- #D012 — apify shared httpx client
- #D020 — HARD_BLOCKED regex combinada (single search)
- #D025 — save_session JSON compacto + cleanup probabilistico
- #027/#072 — glob_files iterator + skip noise dirs + sort subset
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── #D006 — git regex module-level ────────────────────────────────


class TestGitRegexModuleLevel:
    def test_dangerous_fmt_at_module_scope(self):
        from alpha.tools import git_tools

        assert hasattr(git_tools, "_DANGEROUS_GIT_FMT")

    def test_sanitize_uses_module_level_compiled_regex(self):
        # #D006: o padrao perigoso e compilado uma vez no escopo do modulo
        # (objeto re.Pattern reutilizado) em vez de re-compilado por chamada.
        import re

        from alpha.tools import git_tools

        assert isinstance(git_tools._DANGEROUS_GIT_FMT, re.Pattern)

        # _sanitize_git_args deve usar exatamente esse objeto compartilhado:
        # espionamos .search e confirmamos que a validacao o consulta.
        calls = {"n": 0}
        orig_search = git_tools._DANGEROUS_GIT_FMT.search

        class Spy:
            def search(self, s):
                calls["n"] += 1
                return orig_search(s)

        from alpha.tools.git_tools import _sanitize_git_args

        with patch.object(git_tools, "_DANGEROUS_GIT_FMT", Spy()):
            _sanitize_git_args("log", "--format=%(if)evil%(end)")
        assert calls["n"] >= 1, "sanitize nao consultou o regex module-level"

    def test_dangerous_format_still_blocked(self):
        from alpha.tools.git_tools import _sanitize_git_args

        _, err = _sanitize_git_args("log", "--format=%(if)evil%(end)")
        assert err is not None


# ─── #D009 — DDGS shared instance ──────────────────────────────────


class TestDDGSShared:
    def test_get_ddgs_returns_same_instance(self):
        from alpha.web_search import _get_ddgs

        a = _get_ddgs()
        b = _get_ddgs()
        assert a is b


# ─── #D025 — save_session compact JSON ─────────────────────────────


class TestSaveSessionCompact:
    def test_session_file_no_indent(self, tmp_path, monkeypatch):
        from alpha import history

        monkeypatch.setattr(history, "_HISTORY_DIR", tmp_path)
        path = history.save_session(
            "test_compact",
            [{"role": "user", "content": "hello"}],
        )
        text = path.read_text()
        # Sem indent=2 a saida nao tem newlines em meio a estrutura
        assert "\n  " not in text  # 2-space indent ausente

    def test_cleanup_runs_only_on_lucky_draw(self, tmp_path, monkeypatch):
        # #D025: _cleanup_old_sessions roda em ~10% das chamadas
        # (secrets.randbelow(10) == 0), nao em toda save.
        from alpha import history

        monkeypatch.setattr(history, "_HISTORY_DIR", tmp_path)
        cleanup_calls = {"n": 0}
        monkeypatch.setattr(history, "_cleanup_old_sessions",
                            lambda: cleanup_calls.__setitem__("n", cleanup_calls["n"] + 1))

        msgs = [{"role": "user", "content": "hi"}]

        # Draw "azarado" (!=0) -> nao roda cleanup.
        monkeypatch.setattr(history.secrets, "randbelow", lambda n: 7)
        history.save_session("s_no_cleanup", msgs)
        assert cleanup_calls["n"] == 0

        # Draw "sortudo" (==0) -> roda cleanup.
        monkeypatch.setattr(history.secrets, "randbelow", lambda n: 0)
        history.save_session("s_cleanup", msgs)
        assert cleanup_calls["n"] == 1


# ─── #D007 — list_sessions partial read ────────────────────────────


class TestListSessionsPartialRead:
    def test_small_session_skips_full_read(self, tmp_path, monkeypatch):
        # #D007: para sessoes pequenas (< 8KB) o metadata sai do chunk
        # parcial — read_text (full read) NAO deve ser acionado.
        from alpha import history

        monkeypatch.setattr(history, "_HISTORY_DIR", tmp_path)
        history.save_session("small", [{"role": "user", "content": "oi"}])

        full_reads = {"n": 0}
        orig_read_text = Path.read_text

        def spy_read_text(self, *a, **k):
            full_reads["n"] += 1
            return orig_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", spy_read_text)

        sessions = history.list_sessions(limit=10)
        assert any(s["session_id"] == "small" for s in sessions)
        # Partial read bastou — sem fallback de read completo.
        assert full_reads["n"] == 0

    def test_works_with_large_session(self, tmp_path, monkeypatch):
        from alpha import history

        monkeypatch.setattr(history, "_HISTORY_DIR", tmp_path)

        # Cria sessao grande (> 8KB) — deve cair no fallback de read completo
        big_messages = [{"role": "user", "content": "x" * 200}] * 100
        history.save_session("big", big_messages)

        sessions = history.list_sessions(limit=10)
        assert len(sessions) >= 1
        assert any(s["session_id"] == "big" for s in sessions)


# ─── #D010 — composite rglob unified ───────────────────────────────


class TestCompositeRglobUnified:
    @pytest.mark.asyncio
    async def test_detects_both_naming_conventions_single_pass(self, tmp_path, monkeypatch):
        # #D010: deteccao de testes usa um unico rglob("*.py") + filtro por
        # nome, cobrindo TANTO test_*.py QUANTO *_test.py. Antes eram dois
        # rglob separados. Provamos cobertura das duas convencoes.
        from alpha.tools import _composite_tests as ct

        async def fake_run_tool(name, **kw):
            return {"ok": True, "stdout": "", "command": kw.get("command")}

        monkeypatch.setattr(ct, "_run_tool", fake_run_tool)
        monkeypatch.setattr(ct, "_resolve_target", lambda p, **k: (tmp_path, None))

        # Apenas a 2a convencao (*_test.py), sem markers de framework.
        (tmp_path / "alpha_test.py").write_text("def test_x(): pass\n")
        res = await ct._run_tests(str(tmp_path))
        assert res.get("framework") == "pytest", "deteccao perdeu *_test.py"

        # Limpa e testa a 1a convencao (test_*.py).
        (tmp_path / "alpha_test.py").unlink()
        (tmp_path / "test_alpha.py").write_text("def test_x(): pass\n")
        res = await ct._run_tests(str(tmp_path))
        assert res.get("framework") == "pytest", "deteccao perdeu test_*.py"

    @pytest.mark.asyncio
    async def test_skips_noise_dirs_in_detection(self, tmp_path, monkeypatch):
        # Test files dentro de node_modules/.git nao contam como deteccao.
        from alpha.tools import _composite_tests as ct

        monkeypatch.setattr(ct, "_resolve_target", lambda p, **k: (tmp_path, None))
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "test_vendor.py").write_text("")

        res = await ct._run_tests(str(tmp_path))
        # Sem testes reais -> nao detecta pytest, retorna erro de framework.
        assert res.get("ok") is False or "error" in res


# ─── #D012 — apify shared client ───────────────────────────────────


class TestApifySharedClient:
    async def test_get_apify_client_returns_same(self):

        from alpha.tools import apify_tools

        # Module-level client + lazy init
        assert hasattr(apify_tools, "_get_apify_client")
        # Sem loop ativo, get sem comparacao retorna o mesmo
        c1 = await apify_tools._get_apify_client()
        c2 = await apify_tools._get_apify_client()
        assert c1 is c2

    async def test_run_actor_uses_shared_client(self, monkeypatch):
        # #D012: _run_actor obtem o client via _get_apify_client (compartilhado)
        # em vez de criar um httpx.AsyncClient novo por chamada.
        import httpx

        from alpha.tools import apify_tools

        monkeypatch.setenv("APIFY_API_TOKEN", "fake-token")

        get_client_calls = {"n": 0}
        # Client mockado: post retorna shape valido com run id; sem dataset
        # nos faz a funcao terminar cedo apos detectar shape.
        fake_client = MagicMock()

        async def fake_post(url, **kw):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            # shape invalido -> funcao retorna logo apos validar, sem polling.
            resp.json = MagicMock(return_value={"data": "not-a-dict"})
            return resp

        fake_client.post = fake_post

        async def fake_get_client(timeout=30.0):
            get_client_calls["n"] += 1
            return fake_client

        # Se o codigo criasse seu proprio client, este sentinel falharia.
        def boom_client(*a, **k):
            raise AssertionError("_run_actor criou httpx.AsyncClient proprio")

        monkeypatch.setattr(apify_tools, "_get_apify_client", fake_get_client)
        monkeypatch.setattr(httpx, "AsyncClient", boom_client)

        await apify_tools._run_actor("my-actor")
        assert get_client_calls["n"] == 1


# ─── #D020 — HARD_BLOCKED combined regex ───────────────────────────


class TestHardBlockedCombinedRegex:
    def test_combined_regex_exists(self):
        from alpha.tools import shell_tools

        assert hasattr(shell_tools, "HARD_BLOCKED_RE")
        # Continua valida — fork bomb e padrao inequivocamente perigoso
        assert shell_tools.HARD_BLOCKED_RE.search(":(){:|:&};:")

    def test_validate_consults_combined_regex_once(self, monkeypatch):
        # #D020: _validate_command checa o comando com um unico
        # HARD_BLOCKED_RE.search em vez de iterar sobre N patterns.
        from alpha.tools import shell_tools

        # _validate_command vive em alpha.security (re-exportado por
        # shell_tools); o regex que ele consulta esta nesse namespace.
        sec_module = sys.modules[shell_tools._validate_command.__module__]

        searches = {"n": 0}
        real_re = sec_module.HARD_BLOCKED_RE

        class SpyRe:
            def search(self, s):
                searches["n"] += 1
                return real_re.search(s)

        monkeypatch.setattr(sec_module, "HARD_BLOCKED_RE", SpyRe())
        # Comando seguro: deve passar pelo unico search e nao bloquear.
        assert shell_tools._validate_command("echo hi") is None
        assert searches["n"] == 1, "esperava UM unico search combinado"

    def test_dangerous_commands_still_blocked(self):
        from alpha.tools.shell_tools import _validate_command

        # Casos que devem continuar bloqueados
        for cmd in ("rm -rf /", "mkfs.ext4 /dev/sda", "shutdown -h now"):
            assert _validate_command(cmd) is not None, f"Expected block: {cmd}"

    def test_safe_commands_still_pass(self):
        from alpha.tools.shell_tools import _validate_command

        for cmd in ("echo hello", "ls -la", "git status", "python -c 'print(1)'"):
            assert _validate_command(cmd) is None, f"Expected allow: {cmd}"


# ─── #027/#072 — glob_files iterator + skip ────────────────────────


class TestGlobFilesIterator:
    @pytest.mark.asyncio
    async def test_glob_skips_node_modules(self, tmp_path, monkeypatch):
        from alpha.tools import file_tools, path_helpers, workspace as ws_module

        # Estrutura: workspace/{src/a.py, node_modules/foo.js, .git/HEAD}
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "foo.py").write_text("")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "x.py").write_text("")

        monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(file_tools, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(path_helpers, "AGENT_WORKSPACE", tmp_path)

        result = await file_tools._glob_files("**/*.py", str(tmp_path))
        paths = [m["path"] for m in result["matches"]]
        rel_paths = [str(Path(p).relative_to(tmp_path)) for p in paths]
        # Deve achar src/a.py mas nao node_modules ou .git
        assert any("a.py" in p for p in rel_paths)
        assert not any("node_modules" in p for p in rel_paths)
        assert not any(p.startswith(".git") for p in rel_paths)
