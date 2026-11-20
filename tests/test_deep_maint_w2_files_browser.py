"""Testes da wave DEEP_MAINTAINABILITY (w2) — file_tools + browser tools.

Cobre os refactors preservadores de comportamento:

- #DM070: `_RG_EXCLUDES` derivado de `_SKIP_DIRS` (fonte única) + cauda de
  escrita atômica compartilhada (`_overwrite_validated`).
- #DM077: mensagem Playwright única, `_compile_user_regex`, tab tools
  reusando `_require_session`, `_BrowserLoop.alive()` reusado.
- #DM075 (browser): `_VIEWPORT`/`_CONTEXT_KWARGS` compartilhados.
"""

from __future__ import annotations

import asyncio

import pytest

# browser_tools precisa carregar antes de _browser_tabs (back-import
# intencional documentado no docstring de _browser_tabs).
from alpha.tools import browser_session, browser_tools, file_tools
from alpha.tools import _browser_tabs  # noqa: E402


# ─── #DM070: excludes derivados de _SKIP_DIRS ────────────────────────


class TestRgExcludesDerivedFromSkipDirs:
    def test_rg_excludes_matches_skip_dirs_exactly(self):
        # O invariante documentado no docstring do módulo: a busca honra
        # `_SKIP_DIRS`. Derivação garante que editar _SKIP_DIRS propaga.
        assert set(file_tools._RG_EXCLUDES) == set(file_tools._SKIP_DIRS)

    def test_previous_excludes_still_present(self):
        # Não regride o conjunto antigo (busca continua pulando estes).
        for d in (".git", "node_modules", "__pycache__", ".venv"):
            assert d in file_tools._RG_EXCLUDES

    @pytest.mark.asyncio
    async def test_search_skips_dist_and_build_now(self, tmp_path, monkeypatch):
        # Comportamento novo-intencional (#DM070): dist/build (membros de
        # _SKIP_DIRS) agora são pulados pela busca também no fallback Python.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("hello alpha\n")
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "bundle.js").write_text("hello alpha\n")
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "out.txt").write_text("hello alpha\n")
        monkeypatch.setattr(file_tools, "_RIPGREP_BIN", None)
        monkeypatch.setattr(file_tools, "_validate_path", lambda p: tmp_path)
        from pathlib import Path

        result = await file_tools._search_files("hello alpha", str(tmp_path))
        rel = {
            Path(r["file"]).relative_to(tmp_path).parts[0]
            for r in result["results"]
        }
        assert rel == {"src"}


# ─── #DM070 sub-achado: cauda de escrita atômica compartilhada ───────


class TestOverwriteValidatedHelper:
    def test_write_flags_include_nofollow_trunc(self):
        import os

        assert file_tools._WRITE_FLAGS & os.O_NOFOLLOW
        assert file_tools._WRITE_FLAGS & os.O_TRUNC
        assert file_tools._WRITE_FLAGS & os.O_CREAT

    @pytest.mark.asyncio
    async def test_write_file_roundtrip(self, tmp_path, monkeypatch):
        target = tmp_path / "novo.txt"
        monkeypatch.setattr(
            file_tools, "_validate_path_no_symlink", lambda p: target
        )
        monkeypatch.setattr(
            file_tools, "assert_within_workspace", lambda p: None
        )
        result = await file_tools._write_file(str(target), "conteúdo")
        assert result["path"] == str(target)
        assert result["bytes_written"] == len("conteúdo".encode("utf-8"))
        assert target.read_text() == "conteúdo"

    @pytest.mark.asyncio
    async def test_edit_file_roundtrip(self, tmp_path, monkeypatch):
        target = tmp_path / "ed.txt"
        target.write_text("aaa bbb ccc")
        monkeypatch.setattr(
            file_tools, "_validate_path_no_symlink", lambda p: target
        )
        monkeypatch.setattr(
            file_tools, "assert_within_workspace", lambda p: None
        )
        result = await file_tools._edit_file(str(target), "bbb", "XXX")
        assert result == {
            "path": str(target),
            "occurrences_found": 1,
            "replaced": 1,
        }
        assert target.read_text() == "aaa XXX ccc"

    @pytest.mark.asyncio
    async def test_write_outside_workspace_denied(self, tmp_path, monkeypatch):
        target = tmp_path / "fora.txt"
        monkeypatch.setattr(
            file_tools, "_validate_path_no_symlink", lambda p: target
        )
        monkeypatch.setattr(
            file_tools, "assert_within_workspace", lambda p: "fora do workspace"
        )
        result = await file_tools._write_file(str(target), "x")
        # Mesma semântica de antes do refactor: o PermissionError levantado
        # na cauda de escrita é OSError → mapeado para "Erro de I/O: ...".
        assert "fora do workspace" in result["error"]
        assert not target.exists()


# ─── #DM077: browser guards/mensagens unificados ─────────────────────


class TestPlaywrightMessageUnified:
    def test_check_available_uses_shared_constant(self, monkeypatch):
        monkeypatch.setattr(browser_tools, "PLAYWRIGHT_AVAILABLE", False)
        err = browser_tools._check_available()
        assert err == {"error": browser_session.PLAYWRIGHT_INSTALL_MSG}

    def test_message_wording_preserved(self):
        assert browser_session.PLAYWRIGHT_INSTALL_MSG == (
            "Playwright not installed. Run: "
            "pip install playwright && playwright install chromium"
        )

    @pytest.mark.asyncio
    async def test_open_raises_install_msg_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(browser_session, "PLAYWRIGHT_AVAILABLE", False)
        sess = browser_session.BrowserSession()
        with pytest.raises(RuntimeError, match="Playwright not installed"):
            await sess.open()


class TestCompileUserRegex:
    def test_valid_pattern(self):
        rx, err = browser_tools._compile_user_regex(r"foo\d+")
        assert err is None
        assert rx.search("foo123")

    def test_invalid_pattern_message_preserved(self):
        rx, err = browser_tools._compile_user_regex(r"foo(")
        assert rx is None
        assert err["error"].startswith("Regex inválida: ")

    @pytest.mark.asyncio
    async def test_read_console_invalid_regex(self, monkeypatch):
        async def fake_require_session():
            class _S:
                console_logs = []

            return _S(), None

        monkeypatch.setattr(browser_tools, "_require_session", fake_require_session)
        result = await browser_tools._browser_read_console(pattern="(")
        assert result["error"].startswith("Regex inválida: ")

    @pytest.mark.asyncio
    async def test_read_network_invalid_regex(self, monkeypatch):
        async def fake_require_session():
            class _S:
                network_events = []

            return _S(), None

        monkeypatch.setattr(browser_tools, "_require_session", fake_require_session)
        result = await browser_tools._browser_read_network(url_pattern="(")
        assert result["error"].startswith("Regex inválida: ")


class TestTabToolsReuseRequireSession:
    @pytest.mark.asyncio
    async def test_list_tabs_closed_session_message_unified(self, monkeypatch):
        # #DM077(c): antes retornava "Sessão não aberta." — agora o wording
        # canônico de _require_session.
        monkeypatch.setattr(browser_tools, "PLAYWRIGHT_AVAILABLE", True)

        class _Closed:
            def is_open(self):
                return False

        async def fake_get():
            return _Closed()

        monkeypatch.setattr(
            browser_tools.BrowserSession, "get", staticmethod(fake_get)
        )
        for tool in (
            _browser_tabs._browser_list_tabs(),
            _browser_tabs._browser_switch_tab(0),
            _browser_tabs._browser_close_tab(),
        ):
            result = await tool
            assert result == {
                "error": "Sessão de navegador não aberta. Use browser_open primeiro."
            }

    @pytest.mark.asyncio
    async def test_tab_tools_blocked_without_playwright(self, monkeypatch):
        monkeypatch.setattr(browser_tools, "PLAYWRIGHT_AVAILABLE", False)
        result = await _browser_tabs._browser_list_tabs()
        assert result == {"error": browser_session.PLAYWRIGHT_INSTALL_MSG}


class TestBrowserLoopAliveReuse:
    def test_loop_start_alive_stop_cycle(self):
        loop_cls = browser_session._BrowserLoop
        loop_cls.stop()
        assert not loop_cls.alive()
        loop = loop_cls.loop()
        try:
            assert loop_cls.alive()
            # loop() com thread viva devolve o MESMO loop (fast-path via alive()).
            assert loop_cls.loop() is loop
            fut = loop_cls.submit(asyncio.sleep(0, result=42))
            assert fut.result(timeout=5) == 42
        finally:
            loop_cls.stop()
        assert not loop_cls.alive()
        assert loop_cls._loop is None and loop_cls._thread is None


# ─── #DM075 (browser): viewport/context kwargs compartilhados ────────


class TestContextKwargsConstants:
    def test_viewport_values_preserved(self):
        assert browser_session._VIEWPORT == {"width": 1280, "height": 800}

    def test_context_kwargs_shape(self):
        assert browser_session._CONTEXT_KWARGS == {
            "viewport": browser_session._VIEWPORT,
            "accept_downloads": False,
            "java_script_enabled": True,
        }
