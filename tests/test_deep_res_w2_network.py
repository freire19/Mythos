"""Regressao DEEP_RESILIENCE wave 2 (rede) — #D802, #D810, #D803, #D815.

- #D802: pre-check de URL em `_http_request` e sintatico (sem getaddrinfo
  na thread do loop); `resolve_and_validate` tem timeout e traduz
  gaierror/OSError em ValueError (fail-closed).
- #D810: redirect manual (branch aiohttp) remove headers hop-sensitive
  cross-host, preserva body em 307/308 e faz POST->GET em 301/302/303.
- #D803/#D815: caps de streaming (web_search._fetch_raw e branch aiohttp
  de _http_request) abortam a leitura ao passar do limite.
"""

from __future__ import annotations

import socket
import time
import types

import pytest

from alpha import net_utils, web_search
from alpha.net_utils import resolve_and_validate, validate_url, validate_url_syntax
from alpha.tools import network_tools


_PUBLIC_IP = "93.184.216.34"


def _addrinfo(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


# ── #D802: validate_url_syntax — sem DNS ────────────────────────────


class TestValidateUrlSyntax:
    def test_blocks_bad_scheme(self):
        assert validate_url_syntax("ftp://example.com/x") is not None

    def test_blocks_missing_hostname(self):
        assert validate_url_syntax("http:///path") is not None

    def test_blocks_garbage(self):
        assert validate_url_syntax("not a url at all") is not None

    def test_accepts_public_url_without_dns(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("validate_url_syntax nao pode resolver DNS")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        assert validate_url_syntax("https://example.com/path") is None

    def test_validate_url_still_checks_ip(self, monkeypatch):
        monkeypatch.setattr(net_utils, "is_private_ip", lambda h: True)
        err = validate_url("http://evil.example/")
        assert err is not None and "SSRF" in err

    def test_validate_url_public_ok(self, monkeypatch):
        monkeypatch.setattr(net_utils, "is_private_ip", lambda h: False)
        assert validate_url("https://example.com/") is None


# ── #D802: resolve_and_validate — timeout + except amplo ────────────


class TestResolveAndValidate:
    async def test_gaierror_becomes_valueerror(self, monkeypatch):
        def _fail(*a, **kw):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        with pytest.raises(ValueError):
            await resolve_and_validate("nx.example")

    async def test_plain_oserror_becomes_valueerror(self, monkeypatch):
        def _fail(*a, **kw):
            raise OSError("resolver exploded")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        with pytest.raises(ValueError):
            await resolve_and_validate("nx.example")

    async def test_slow_resolver_hits_timeout(self, monkeypatch):
        monkeypatch.setattr(net_utils, "_DNS_TIMEOUT", 0.05)

        def _slow(*a, **kw):
            time.sleep(0.4)
            return _addrinfo(_PUBLIC_IP)

        monkeypatch.setattr(socket, "getaddrinfo", _slow)
        start = time.monotonic()
        with pytest.raises(ValueError):
            await resolve_and_validate("slow.example")
        assert time.monotonic() - start < 0.35

    async def test_private_ip_rejected(self, monkeypatch):
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **kw: _addrinfo("10.0.0.1")
        )
        with pytest.raises(ValueError):
            await resolve_and_validate("internal.example")

    async def test_public_ip_returned(self, monkeypatch):
        monkeypatch.setattr(
            socket, "getaddrinfo", lambda *a, **kw: _addrinfo(_PUBLIC_IP)
        )
        assert await resolve_and_validate("public.example") == _PUBLIC_IP


# ── #D802: _http_request — pre-check sem getaddrinfo no loop ────────


class TestHttpRequestPrecheck:
    async def test_bad_scheme_blocked_without_dns(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("pre-check nao pode resolver DNS")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        result = await network_tools._http_request("ftp://example.com/x")
        assert result.get("blocked") is True

    async def test_private_ip_still_blocked_async(self):
        result = await network_tools._http_request("http://10.0.0.1/api")
        assert result.get("blocked") is True

    def test_blocking_validate_url_removed(self):
        assert not hasattr(network_tools, "_validate_url")
        assert hasattr(network_tools, "_validate_url_syntax")


# ── #D810/#D815: branch aiohttp com session fake ────────────────────


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
    """Instala aiohttp fake + resolucao DNS fake; devolve factory de sessao."""
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", _fake_aiohttp_module())

    async def _resolve(hostname):
        return _PUBLIC_IP

    monkeypatch.setattr(network_tools, "_resolve_and_validate", _resolve)

    def _install(session):
        async def _get():
            return session

        monkeypatch.setattr(network_tools, "_get_shared_aiohttp_session", _get)
        return session

    return _install


class TestRedirectHeaders:
    async def test_cross_host_strips_sensitive_headers(self, aiohttp_env):
        session = aiohttp_env(
            _FakeSession(
                [
                    _FakeResp(302, {"Location": "https://b.example/next"}),
                    _FakeResp(200),
                ]
            )
        )
        result = await network_tools._http_request(
            "https://a.example/x",
            headers={
                "Authorization": "Bearer s3cr3t",
                "Cookie": "sid=1",
                "Proxy-Authorization": "Basic xyz",
                "X-Custom": "keep",
            },
        )
        assert result["status_code"] == 200
        assert len(session.calls) == 2
        second = {k.lower(): v for k, v in session.calls[1]["headers"].items()}
        assert "authorization" not in second
        assert "cookie" not in second
        assert "proxy-authorization" not in second
        assert second["x-custom"] == "keep"
        assert second["host"] == "b.example"

    async def test_same_host_keeps_authorization(self, aiohttp_env):
        session = aiohttp_env(
            _FakeSession(
                [
                    _FakeResp(302, {"Location": "/next"}),
                    _FakeResp(200),
                ]
            )
        )
        result = await network_tools._http_request(
            "https://a.example/x", headers={"Authorization": "Bearer s3cr3t"}
        )
        assert result["status_code"] == 200
        second = {k.lower(): v for k, v in session.calls[1]["headers"].items()}
        assert second["authorization"] == "Bearer s3cr3t"


class TestRedirectMethodAndBody:
    async def test_307_preserves_post_and_body(self, aiohttp_env):
        body = '{"a": 1}'
        session = aiohttp_env(
            _FakeSession(
                [
                    _FakeResp(307, {"Location": "https://a.example/other"}),
                    _FakeResp(200),
                ]
            )
        )
        result = await network_tools._http_request(
            "https://a.example/x", method="POST", body=body
        )
        assert result["status_code"] == 200
        assert session.calls[1]["method"] == "POST"
        assert session.calls[1]["data"] == body

    async def test_303_downgrades_post_to_get(self, aiohttp_env):
        session = aiohttp_env(
            _FakeSession(
                [
                    _FakeResp(303, {"Location": "https://a.example/done"}),
                    _FakeResp(200),
                ]
            )
        )
        result = await network_tools._http_request(
            "https://a.example/x", method="POST", body='{"a": 1}'
        )
        assert result["status_code"] == 200
        assert session.calls[1]["method"] == "GET"
        assert session.calls[1]["data"] is None

    async def test_301_downgrades_post_to_get(self, aiohttp_env):
        session = aiohttp_env(
            _FakeSession(
                [
                    _FakeResp(301, {"Location": "https://a.example/moved"}),
                    _FakeResp(200),
                ]
            )
        )
        await network_tools._http_request(
            "https://a.example/x", method="POST", body="payload"
        )
        assert session.calls[1]["method"] == "GET"
        assert session.calls[1]["data"] is None


class TestAiohttpStreamingCap:
    async def test_body_capped_and_read_aborted(self, aiohttp_env):
        chunk = b"x" * 100_000
        resp = _FakeResp(200, chunks=[chunk] * 20)  # 2MB total
        session = aiohttp_env(_FakeSession([resp]))
        result = await network_tools._http_request("https://a.example/big")
        assert result["status_code"] == 200
        # cap de 1MB -> aborta apos ~11 chunks, longe dos 20 totais
        assert resp.content.consumed <= 12
        assert len(session.calls) == 1


# ── #D803: web_search._fetch_raw — cap streaming ────────────────────


class _FakeStreamResp:
    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.consumed = 0

    async def aiter_bytes(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeHttpxClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def stream(self, method, url, headers=None, extensions=None):
        self.calls.append((method, url))
        return _FakeStreamCM(self._responses.pop(0))


class TestFetchRawStreamingCap:
    async def test_body_capped_and_read_aborted(self, monkeypatch):
        async def _resolve(hostname):
            return _PUBLIC_IP

        monkeypatch.setattr(web_search, "_resolve_and_validate", _resolve)
        resp = _FakeStreamResp(200, chunks=[b"y" * 10_000] * 50)  # 500KB total
        client = _FakeHttpxClient([resp])

        async def _get_client():
            return client

        monkeypatch.setattr(web_search, "_get_shared_client", _get_client)
        raw, headers, status = await web_search._fetch_raw(
            "https://a.example/big", timeout=5.0, max_bytes=100_000
        )
        assert status == 200
        assert len(raw) == 100_000
        # abortou logo apos passar o cap, sem drenar os 50 chunks
        assert resp.consumed <= 12
