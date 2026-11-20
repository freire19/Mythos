"""Regression tests for DEEP_RESILIENCE V1.0/V1.1 stragglers batch 2.

Cobre:
- #053 — compress_context error handling distingue timeout vs bug
- #055 — atexit shutdown_browser detecta loop ativo
- #057 — http_request retry de transientes em metodos seguros
- #051/#D012 — apify polling loga e aborta apos N consecutivos
- #065 — listener `_on_new_page` removido em close
- #067 — SIGTERM handler instalado
- #D010 — extract_multiple_pages log finalizado com contagem
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── #053 — compress_context error handling ────────────────────────


class TestCompressContextErrorHandling:
    @pytest.mark.asyncio
    async def test_timeout_is_warned_and_swallowed(self, monkeypatch, caplog):
        # #DM038: _maybe_compress trata TimeoutError como soft (warning +
        # continue) — o agent loop sobrevive a um LLM de compressao lento.
        from alpha import agent

        monkeypatch.setattr(agent, "needs_compression", lambda *a, **k: True)
        monkeypatch.setattr(agent, "estimate_messages_tokens", lambda *a, **k: 100)

        async def boom_timeout(*a, **k):
            raise TimeoutError("compress LLM slow")

        monkeypatch.setattr(agent, "compress_until_under_budget", boom_timeout)

        with caplog.at_level(logging.WARNING, logger="alpha.agent"):
            events = [ev async for ev in agent._maybe_compress([{"role": "user", "content": "x"}], "openai")]

        # Nenhum evento emitido (compressao falhou), mas nao propagou.
        assert events == []
        assert any(r.levelno == logging.WARNING and "timeout" in r.getMessage().lower()
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unexpected_bug_uses_exception_with_traceback(self, monkeypatch, caplog):
        # #053: bugs reais em context.py precisam de exc_info (traceback)
        # via logger.exception, nao um warning silencioso.
        from alpha import agent

        monkeypatch.setattr(agent, "needs_compression", lambda *a, **k: True)
        monkeypatch.setattr(agent, "estimate_messages_tokens", lambda *a, **k: 100)

        async def boom_bug(*a, **k):
            raise ValueError("real bug in compression")

        monkeypatch.setattr(agent, "compress_until_under_budget", boom_bug)

        with caplog.at_level(logging.ERROR, logger="alpha.agent"):
            events = [ev async for ev in agent._maybe_compress([{"role": "user", "content": "x"}], "openai")]

        assert events == []
        # logger.exception -> nivel ERROR com exc_info preenchido.
        err_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert err_records, "esperava log de erro"
        assert any(r.exc_info is not None for r in err_records), "traceback nao preservado"


# ─── #055 — atexit shutdown loop detection ─────────────────────────


class TestShutdownLoopDetection:
    def test_runs_shutdown_when_no_loop_active(self, monkeypatch):
        # Sem loop ativo, asyncio.run executa shutdown_browser direto.
        from alpha.cli import lifecycle
        import alpha.tools.browser_session as bs

        called = {"n": 0}

        async def fake_shutdown():
            called["n"] += 1

        monkeypatch.setattr(bs, "shutdown_browser", fake_shutdown)
        lifecycle._shutdown_browser_session()
        assert called["n"] == 1

    def test_runs_shutdown_inside_active_loop(self, monkeypatch):
        # Quando get_running_loop() detecta um loop ativo (caso atexit em
        # testes/embedding), o shutdown nao pode explodir — cria um loop
        # dedicado e ainda fecha a sessao.
        from alpha.cli import lifecycle
        import alpha.tools.browser_session as bs

        called = {"n": 0}

        async def fake_shutdown():
            called["n"] += 1

        monkeypatch.setattr(bs, "shutdown_browser", fake_shutdown)
        # Simula "ha um loop rodando" sem realmente estar dentro dele, para
        # que o loop dedicado consiga run_until_complete na mesma thread.
        sentinel_loop = object()
        monkeypatch.setattr(lifecycle.asyncio, "get_running_loop",
                            lambda: sentinel_loop)
        # asyncio.run nao deve ser usado neste caminho — se for, falha.
        monkeypatch.setattr(
            lifecycle.asyncio, "run",
            MagicMock(side_effect=AssertionError("nao deveria usar asyncio.run com loop ativo")),
        )

        lifecycle._shutdown_browser_session()
        assert called["n"] == 1

    def test_import_error_is_silent(self, monkeypatch):
        # Playwright ausente: ImportError engolido, sem stderr.
        from alpha.cli import lifecycle

        def fake_stderr(msg):
            raise AssertionError(f"ImportError nao deveria logar: {msg!r}")

        monkeypatch.setattr(lifecycle, "_stderr", fake_stderr)
        # Simula modulo ausente.
        import builtins
        real_import = builtins.__import__

        def blocked_import(name, *a, **k):
            if name == "alpha.tools.browser_session":
                raise ImportError("no playwright")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", blocked_import)
        lifecycle._shutdown_browser_session()  # nao deve levantar nem logar

    def test_real_error_goes_to_stderr(self, monkeypatch):
        # Erro real (nao ImportError) durante shutdown vai pra stderr em
        # vez de ser engolido por um `except: pass`.
        from alpha.cli import lifecycle
        import alpha.tools.browser_session as bs

        logged = []
        monkeypatch.setattr(lifecycle, "_stderr", lambda msg: logged.append(msg))

        async def boom():
            raise RuntimeError("browser crashed")

        monkeypatch.setattr(bs, "shutdown_browser", boom)
        lifecycle._shutdown_browser_session()
        assert any("RuntimeError" in m and "browser crashed" in m for m in logged)


# ─── #057 — http_request retry ──────────────────────────────────────


class TestHttpRequestRetry:
    @pytest.mark.asyncio
    async def test_get_retries_on_transient(self, monkeypatch):
        from alpha.tools import network_tools

        call_count = {"n": 0}

        async def flaky_http(url, method="GET", headers=None, body=None, timeout=None):
            call_count["n"] += 1
            if call_count["n"] < 3:
                return {"error": "Connection reset", "transient": True}
            return {"status_code": 200, "body": "ok"}

        monkeypatch.setattr(network_tools, "_http_request", flaky_http)
        # Patch sleep para nao esperar de verdade
        monkeypatch.setattr(network_tools.asyncio, "sleep",
                            AsyncMock(return_value=None))

        result = await network_tools._http_request_with_retry("https://example.com")
        assert result.get("status_code") == 200
        assert call_count["n"] == 3

    @pytest.mark.asyncio
    async def test_post_does_not_retry(self, monkeypatch):
        from alpha.tools import network_tools

        call_count = {"n": 0}

        async def flaky_http(url, method="GET", headers=None, body=None, timeout=None):
            call_count["n"] += 1
            return {"error": "Connection reset", "transient": True}

        monkeypatch.setattr(network_tools, "_http_request", flaky_http)
        monkeypatch.setattr(network_tools.asyncio, "sleep",
                            AsyncMock(return_value=None))

        # POST nunca retenta — retentar duplicaria criacao de recurso.
        result = await network_tools._http_request_with_retry(
            "https://example.com", method="POST", body="{}"
        )
        assert call_count["n"] == 1
        assert result.get("transient") is True

    @pytest.mark.asyncio
    async def test_retry_budget_exhausted(self, monkeypatch):
        from alpha.tools import network_tools

        call_count = {"n": 0}

        async def always_fails(url, method="GET", headers=None, body=None, timeout=None):
            call_count["n"] += 1
            return {"error": "Conn reset", "transient": True}

        monkeypatch.setattr(network_tools, "_http_request", always_fails)
        monkeypatch.setattr(network_tools.asyncio, "sleep",
                            AsyncMock(return_value=None))

        result = await network_tools._http_request_with_retry("https://example.com")
        # Budget = MAX_RETRIES + 1
        assert call_count["n"] == network_tools.RETRY["http"]["max_retries"] + 1
        assert result.get("retried") == network_tools.RETRY["http"]["max_retries"] + 1


# ─── #051/#D012 — apify polling logging ────────────────────────────


class TestApifyPollLogging:
    @pytest.mark.asyncio
    async def test_aborts_after_consecutive_poll_errors(self, monkeypatch, caplog):
        # #051/#D012: polls que falham 5x consecutivos abortam com erro
        # explicito em vez de pollar inutilmente ate _MAX_WAIT.
        import httpx

        from alpha.tools import apify_tools

        monkeypatch.setenv("APIFY_API_TOKEN", "fake-token")
        monkeypatch.setattr(apify_tools.asyncio, "sleep",
                            AsyncMock(return_value=None))

        poll_calls = {"n": 0}

        class FakeClient:
            async def post(self, url, **kw):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={
                    "data": {"id": "RUN1", "defaultDatasetId": "DS1"}
                })
                return resp

            async def get(self, url, **kw):
                poll_calls["n"] += 1
                raise httpx.ConnectError("apify endpoint offline")

        async def fake_get_client(timeout=30.0):
            return FakeClient()

        monkeypatch.setattr(apify_tools, "_get_apify_client", fake_get_client)

        with caplog.at_level(logging.WARNING, logger=apify_tools.logger.name):
            result = await apify_tools._run_actor("my-actor")

        # Abortou exatamente apos 5 consecutivos, sem chegar perto de _MAX_WAIT.
        assert poll_calls["n"] == 5
        assert "consecutive" in result.get("error", "").lower()
        assert result.get("run_id") == "RUN1"
        assert any(r.levelno >= logging.WARNING and "poll error" in r.getMessage().lower()
                   for r in caplog.records)


# ─── #065 — listener cleanup on close ──────────────────────────────


class TestListenerCleanup:
    @pytest.mark.asyncio
    async def test_close_removes_on_new_page_listener(self):
        # #065: close() deve desregistrar o listener "page" -> _on_new_page,
        # senao callbacks stale empilham e seguram refs ao context.
        from alpha.tools.browser_session import BrowserSession

        session = BrowserSession()
        fake_context = MagicMock()
        session.context = fake_context
        session.browser = None
        session.playwright = None

        await session.close()

        fake_context.remove_listener.assert_called_once_with(
            "page", session._on_new_page
        )


# ─── #067 — SIGTERM handler ────────────────────────────────────────


class TestSigtermHandler:
    def test_install_function_exists(self):
        from alpha.cli import lifecycle

        assert hasattr(lifecycle, "_install_sigterm_handler")

    def test_handler_raises_systemexit_143(self, monkeypatch):
        # Handler precisa SAIR via sys.exit (SystemExit) para disparar os
        # atexit hooks. os._exit pularia atexit — bug conhecido (#067).
        import signal as _signal

        from alpha.cli import lifecycle

        captured = {}

        def fake_signal(signum, handler):
            captured["signum"] = signum
            captured["handler"] = handler

        monkeypatch.setattr(_signal, "signal", fake_signal)
        monkeypatch.setattr(lifecycle, "_stderr", lambda *a, **k: None)

        lifecycle._install_sigterm_handler()

        assert captured["signum"] == _signal.SIGTERM
        handler = captured["handler"]
        # Invocar o handler deve levantar SystemExit com codigo 143 (128+15).
        with pytest.raises(SystemExit) as exc_info:
            handler(_signal.SIGTERM, None)
        assert exc_info.value.code == 143


# ─── #D010 — extract_multiple_pages log ────────────────────────────


class TestExtractMultipleLogFinalize:
    @pytest.mark.asyncio
    async def test_logs_ok_failed_empty_counts(self, monkeypatch, caplog):
        # #D010: apos o gather, loga contagem por outcome (ok/failed/empty)
        # para o caller diagnosticar 0-paginas.
        from alpha import web_search

        async def fake_extract(url, max_chars=8000):
            if url == "https://ok.example":
                return "conteudo"
            if url == "https://empty.example":
                return ""
            raise RuntimeError("network down")

        monkeypatch.setattr(web_search, "extract_page_content", fake_extract)

        urls = ["https://ok.example", "https://empty.example", "https://fail.example"]
        with caplog.at_level(logging.INFO, logger="alpha.web_search"):
            result = await web_search.extract_multiple_pages(urls)

        # Resultado so contem a pagina com texto.
        assert result == {"https://ok.example": "conteudo"}
        summary = [r.getMessage() for r in caplog.records
                   if "extract_multiple_pages" in r.getMessage() and "ok" in r.getMessage()]
        assert summary, "esperava log de sumario"
        msg = summary[-1]
        assert "1/3 ok" in msg
        assert "1 failed" in msg
        assert "1 empty" in msg
