"""Testes da wave DEEP_RESILIENCE W2 — MCP (#D801/#D811/#D812/#D813/#D814).

Sem rede real: httpx.MockTransport / transports fake / clients fake.
"""

from __future__ import annotations

import asyncio
import time
import types

import httpx
import pytest

import alpha.mcp.http_client as http_client_mod
import alpha.mcp.loader as loader
from alpha._turn_budget import use_deadline
from alpha.mcp.client import TOOL_CALL_TIMEOUT, MCPError
from alpha.mcp.http_client import MCPHttpClient

URL = "https://mcp.example.com/mcp"


@pytest.fixture(autouse=True)
def _public_host(monkeypatch):
    monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)


def _make_client(transport) -> MCPHttpClient:
    client = MCPHttpClient(name="test", url=URL, transport=transport)
    client.start()
    return client


# ── #D801 (regressão — coberto por #D312/_read_capped) ───────────


class TestD801ErrorPreviewCapped:
    def test_error_body_preview_is_capped(self):
        huge = b"x" * 1_000_000

        def handler(request):
            return httpx.Response(500, content=huge)

        client = _make_client(httpx.MockTransport(handler))
        with pytest.raises(MCPError) as exc:
            client._request("tools/list")
        # Preview de no máximo ~200 chars — corpo gigante não vai à mensagem.
        assert len(str(exc.value)) < 400


# ── #D811: erros httpx nas leituras pós-_post viram MCPError ─────


class _RaisingStream(httpx.SyncByteStream):
    def __init__(self, exc: Exception):
        self._exc = exc

    def __iter__(self):
        yield b'{"jsonrpc"'
        raise self._exc


class _StreamTransport(httpx.BaseTransport):
    def __init__(self, status: int, exc: Exception, ctype: str = "application/json"):
        self._status = status
        self._exc = exc
        self._ctype = ctype

    def handle_request(self, request):
        return httpx.Response(
            self._status,
            headers={"content-type": self._ctype},
            stream=_RaisingStream(self._exc),
        )


class TestD811ReadErrorsTranslated:
    def test_read_error_on_json_body_becomes_mcperror(self):
        client = _make_client(_StreamTransport(200, httpx.ReadError("reset")))
        with pytest.raises(MCPError) as exc:
            client._request("tools/list")
        assert "test" in str(exc.value) and "tools/list" in str(exc.value)

    def test_read_timeout_on_body_becomes_mcperror(self):
        client = _make_client(_StreamTransport(200, httpx.ReadTimeout("slow")))
        with pytest.raises(MCPError) as exc:
            client._request("tools/list")
        assert "timed out" in str(exc.value)

    def test_read_error_on_sse_becomes_mcperror(self):
        client = _make_client(
            _StreamTransport(200, httpx.ReadError("reset"), ctype="text/event-stream")
        )
        with pytest.raises(MCPError) as exc:
            client._request("tools/list")
        assert "transport error" in str(exc.value)

    def test_read_error_on_error_status_becomes_mcperror(self):
        client = _make_client(_StreamTransport(500, httpx.ReadError("reset")))
        with pytest.raises(MCPError):
            client._request("tools/list")

    def test_read_error_on_notify_becomes_mcperror(self):
        client = _make_client(_StreamTransport(400, httpx.ReadError("reset")))
        with pytest.raises(MCPError):
            client._notify("notifications/initialized")


# ── #D813: budget exaurido → floor fail-fast, não 120s ───────────


class _RecordingClient:
    def __init__(self):
        self.name = "srv"
        self.tools = []
        self.timeouts: list[float] = []

    def call_tool(self, tool_name, arguments, timeout):
        self.timeouts.append(timeout)
        return {"content": [{"type": "text", "text": "ok"}]}


class TestD813BudgetClamp:
    def _run(self, deadline_offset):
        client = _RecordingClient()
        executor = loader._make_executor(client, "t")

        async def go():
            if deadline_offset is None:
                return await executor()
            with use_deadline(time.monotonic() + deadline_offset):
                return await executor()

        asyncio.run(go())
        return client.timeouts[0]

    def test_no_budget_uses_default(self):
        assert self._run(None) == TOOL_CALL_TIMEOUT

    def test_exhausted_budget_fails_fast(self):
        timeout = self._run(-5)
        assert timeout <= 2.0

    def test_positive_budget_clamps(self):
        timeout = self._run(50)
        assert timeout <= 50.1 and timeout < TOOL_CALL_TIMEOUT


# ── #D812: connect-all paralelo com budget agregado ──────────────


class _FakeConnectedClient:
    def __init__(self, name):
        self.name = name
        self.tools = []
        self.stopped = False

    def stop(self):
        self.stopped = True


class TestD812ParallelStartup:
    @pytest.fixture(autouse=True)
    def _reset_loader(self):
        loader._loaded = False
        saved = list(loader._active_clients)
        loader._active_clients.clear()
        yield
        loader._active_clients.clear()
        loader._active_clients.extend(saved)
        loader._loaded = True

    def test_parallel_connect_and_global_budget(self, monkeypatch):
        specs = [
            types.SimpleNamespace(name="fast1", disabled=False),
            types.SimpleNamespace(name="fast2", disabled=False),
            types.SimpleNamespace(name="stuck", disabled=False),
        ]
        monkeypatch.setattr(loader, "load_mcp_config", lambda: specs)
        monkeypatch.setattr(loader, "_MCP_GLOBAL_STARTUP_BUDGET", 0.5)

        def fake_connect(spec, startup_timeout=None):
            if spec.name == "stuck":
                time.sleep(1.5)
            return _FakeConnectedClient(spec.name)

        monkeypatch.setattr(loader, "_connect_one", fake_connect)

        start = time.monotonic()
        clients = loader.load_mcp_servers()
        elapsed = time.monotonic() - start

        assert sorted(c.name for c in clients) == ["fast1", "fast2"]
        assert elapsed < 1.4  # sequencial seria >= 1.5s só pelo "stuck"

    def test_all_fast_connects_registered(self, monkeypatch):
        specs = [
            types.SimpleNamespace(name=f"s{i}", disabled=False) for i in range(4)
        ]
        monkeypatch.setattr(loader, "load_mcp_config", lambda: specs)
        monkeypatch.setattr(
            loader, "_connect_one",
            lambda spec, startup_timeout=None: _FakeConnectedClient(spec.name),
        )
        clients = loader.load_mcp_servers()
        assert sorted(c.name for c in clients) == ["s0", "s1", "s2", "s3"]


# ── #D814: retry só do handshake em erro transitório ─────────────


class TestD814HandshakeRetry:
    def test_transient_detection(self):
        e = MCPError("MCP 's' transport error on 'initialize': boom")
        e.__cause__ = httpx.ConnectError("boom")
        assert loader._transient_transport_error(e) is True
        assert loader._transient_transport_error(
            MCPError("MCP 's' closed connection during 'initialize'")
        ) is True
        assert loader._transient_transport_error(
            MCPError("MCP 's' write failed: broken pipe")
        ) is True
        assert loader._transient_transport_error(
            MCPError("MCP 's' returned error on 'initialize': -32600 bad")
        ) is False

    def _spec(self):
        return types.SimpleNamespace(
            name="s", command="x", args=[], env={}, is_http=False
        )

    def test_retries_once_on_transient_error(self, monkeypatch):
        attempts = {"n": 0}

        class FlakyClient:
            def __init__(self, **kwargs):
                self.name = kwargs.get("name", "s")
                self.tools = []

            def start(self):
                pass

            def initialize(self):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise MCPError("MCP 's' closed connection during 'initialize'")
                return {}

            def list_tools(self):
                return []

            def stop(self):
                pass

        monkeypatch.setattr(loader, "MCPClient", FlakyClient)
        monkeypatch.setattr(loader, "_HANDSHAKE_BACKOFF", 0.0)
        client = loader._connect_one(self._spec())
        assert client is not None
        assert attempts["n"] == 2

    def test_no_retry_on_protocol_error(self, monkeypatch):
        attempts = {"n": 0}

        class BadClient:
            def __init__(self, **kwargs):
                self.name = kwargs.get("name", "s")
                self.tools = []

            def start(self):
                pass

            def initialize(self):
                attempts["n"] += 1
                raise MCPError("MCP 's' returned error on 'initialize': -32600 bad")

            def list_tools(self):
                return []

            def stop(self):
                pass

        monkeypatch.setattr(loader, "MCPClient", BadClient)
        monkeypatch.setattr(loader, "_HANDSHAKE_BACKOFF", 0.0)
        client = loader._connect_one(self._spec())
        assert client is None
        assert attempts["n"] == 1

    def test_transient_error_exhausts_retries(self, monkeypatch):
        attempts = {"n": 0}

        class DeadClient:
            def __init__(self, **kwargs):
                self.name = kwargs.get("name", "s")
                self.tools = []

            def start(self):
                pass

            def initialize(self):
                attempts["n"] += 1
                raise MCPError("MCP 's' closed connection during 'initialize'")

            def list_tools(self):
                return []

            def stop(self):
                pass

        monkeypatch.setattr(loader, "MCPClient", DeadClient)
        monkeypatch.setattr(loader, "_HANDSHAKE_BACKOFF", 0.0)
        client = loader._connect_one(self._spec())
        assert client is None
        assert attempts["n"] == 2
