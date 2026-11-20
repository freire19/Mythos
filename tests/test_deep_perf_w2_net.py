"""DEEP_PERFORMANCE wave 2 — rede (#DP031 skip-check, #DP039).

#DP031: cap de corpo no path aiohttp ja coberto por #DB09/#D314/#D815
(iter_chunked + break); regressao barata aqui.
#DP039: contexto SSL default cacheado no modulo e reusado em request e
redirect, sem mudar comportamento (SNI via server_hostname preservado).
"""

from __future__ import annotations

import ssl
import sys
import types

import pytest

from alpha.tools import network_tools

pytestmark = pytest.mark.anyio

_PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── fakes aiohttp (mesmo padrao de test_deep_res_w2_network.py) ─────


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.consumed = 0

    def iter_chunked(self, size):
        async def _gen():
            for chunk in self._chunks:
                self.consumed += 1
                yield chunk

        return _gen()


class _FakeResp:
    def __init__(self, status, headers=None, chunks=(b"ok",), url="https://x/"):
        self.status = status
        self.headers = dict(headers or {})
        self.content = _FakeContent(chunks)
        self.url = url
        self.released = False

    def release(self):
        self.released = True


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _fake_aiohttp_module():
    mod = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class ClientConnectionError(Exception):
        pass

    class ServerDisconnectedError(ClientConnectionError):
        pass

    class ClientPayloadError(Exception):
        pass

    mod.ClientTimeout = ClientTimeout
    mod.ClientConnectionError = ClientConnectionError
    mod.ServerDisconnectedError = ServerDisconnectedError
    mod.ClientPayloadError = ClientPayloadError
    return mod


@pytest.fixture
def aiohttp_env(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiohttp", _fake_aiohttp_module())

    async def _resolve(hostname):
        return _PUBLIC_IP

    monkeypatch.setattr(network_tools, "_resolve_and_validate", _resolve)

    def _install(session):
        async def _get():
            return session

        monkeypatch.setattr(network_tools, "_get_shared_aiohttp_session", _get)
        return session

    return _install


@pytest.fixture
def ssl_spy(monkeypatch):
    """Zera o cache do modulo e conta chamadas a ssl.create_default_context."""
    monkeypatch.setattr(network_tools, "_DEFAULT_SSL_CTX", None)
    calls = {"n": 0}
    real = ssl.create_default_context

    def spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(network_tools._ssl, "create_default_context", spy)
    return calls


# ── #DP039 ──────────────────────────────────────────────────────────


class TestDefaultSslContextCache:
    def test_same_object_and_single_load(self, ssl_spy):
        ctx1 = network_tools._get_default_ssl_context()
        ctx2 = network_tools._get_default_ssl_context()
        assert ctx1 is ctx2
        assert isinstance(ctx1, ssl.SSLContext)
        assert ssl_spy["n"] == 1

    def test_context_verifies_by_default(self, ssl_spy):
        ctx = network_tools._get_default_ssl_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    async def test_https_request_and_redirect_share_context(
        self, aiohttp_env, ssl_spy
    ):
        session = aiohttp_env(
            _FakeSession(
                [
                    _FakeResp(302, {"Location": "https://b.example/next"}),
                    _FakeResp(200),
                ]
            )
        )
        result = await network_tools._http_request("https://a.example/x")
        assert result["status_code"] == 200
        assert len(session.calls) == 2
        first_ssl = session.calls[0]["ssl"]
        second_ssl = session.calls[1]["ssl"]
        assert isinstance(first_ssl, ssl.SSLContext)
        assert first_ssl is second_ssl
        assert ssl_spy["n"] == 1
        # SNI/validacao por request preservados via server_hostname.
        assert session.calls[0]["server_hostname"] == "a.example"
        assert session.calls[1]["server_hostname"] == "b.example"

    async def test_http_request_keeps_ssl_false(self, aiohttp_env, ssl_spy):
        session = aiohttp_env(_FakeSession([_FakeResp(200)]))
        result = await network_tools._http_request("http://a.example/x")
        assert result["status_code"] == 200
        assert session.calls[0]["ssl"] is False
        assert "server_hostname" not in session.calls[0]
        assert ssl_spy["n"] == 0


# ── #DP031 (skipped: ja coberto por #DB09/#D314) — regressao ────────


class TestAiohttpBodyCapRegression:
    async def test_large_body_capped_and_read_aborted(self, aiohttp_env):
        chunk = b"x" * 65536
        n_chunks = 64  # 4MB disponiveis; cap = 1MB
        resp = _FakeResp(200, chunks=[chunk] * n_chunks)
        aiohttp_env(_FakeSession([resp]))
        result = await network_tools._http_request("http://a.example/big")
        assert result["status_code"] == 200
        assert len(result["body"]) <= 15000
        # streaming abortou no cap — nao consumiu os 64 chunks
        assert resp.content.consumed < n_chunks
        assert resp.released is True

    async def test_small_body_untouched(self, aiohttp_env):
        resp = _FakeResp(200, chunks=[b"hello ", b"world"])
        aiohttp_env(_FakeSession([resp]))
        result = await network_tools._http_request("http://a.example/small")
        assert result["body"] == "hello world"
        assert "truncado" not in result["body"]
