"""OAuth 2.1 interativo para MCP remoto (parity wave 2026-11, item 10).

Servidor de autorização FALSO via httpx.MockTransport (discovery, registro
dinâmico, token endpoint); o callback do navegador é um listener REAL no
loopback que o "opener" de teste aciona numa thread. Contratos:

- discovery via WWW-Authenticate resource_metadata → protected resource →
  authorization server metadata (fallback origin);
- registro dinâmico quando não há client_id; PKCE S256 + state + resource;
- token salvo em ~/.alpha/mcp_tokens.json (0600) e carregado no start()
  do client HTTP (header Authorization: Bearer); refresh silencioso quando
  expirado; 401 sem header estático → MCPAuthRequired com hint /mcp auth;
- /mcp auth|logout <server> no REPL; headless/sem TTY não abre navegador.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import alpha.mcp.http_client as http_client_mod
from alpha.mcp import oauth
from alpha.mcp.http_client import MCPAuthRequired, MCPHttpClient

MCP_URL = "https://mcp.example.com/mcp"
AS = "https://auth.example.com"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "_store_path", lambda: tmp_path / "mcp_tokens.json")
    monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)
    monkeypatch.delenv("ALPHA_MCP_OAUTH", raising=False)


class _FakeAuthServer:
    """MockTransport handler for the AS + the MCP server itself."""

    def __init__(self, *, with_registration=True, require_token=True, expires_in=3600):
        self.with_registration = with_registration
        self.require_token = require_token
        self.expires_in = expires_in
        self.codes: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.seen: list[str] = []
        self.registered: list[dict] = []
        self.refreshes = 0
        self.mcp_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(f"{request.method} {url}")
        path = request.url.path
        if url.startswith(MCP_URL):
            return self._mcp(request)
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(200, json={
                "resource": MCP_URL, "authorization_servers": [AS],
                "scopes_supported": ["mcp:tools"],
            })
        if path == "/.well-known/oauth-authorization-server":
            meta = {
                "issuer": AS,
                "authorization_endpoint": f"{AS}/authorize",
                "token_endpoint": f"{AS}/token",
                "code_challenge_methods_supported": ["S256"],
            }
            if self.with_registration:
                meta["registration_endpoint"] = f"{AS}/register"
            return httpx.Response(200, json=meta)
        if path == "/register":
            body = json.loads(request.content)
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": "cid-123"})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") == "authorization_code":
                issued = self.codes.get(form.get("code", ""))
                if not issued or form.get("code_verifier") != issued["verifier"] \
                        or form.get("client_id") != "cid-123" or form.get("resource") != MCP_URL:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.tokens.add("at-1")
                return httpx.Response(200, json={
                    "access_token": "at-1", "token_type": "Bearer",
                    "expires_in": self.expires_in, "refresh_token": "rt-1",
                })
            if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "rt-1":
                self.refreshes += 1
                self.tokens.add("at-2")
                return httpx.Response(200, json={
                    "access_token": "at-2", "token_type": "Bearer", "expires_in": 3600,
                })
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)

    def _mcp(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if self.require_token and auth.replace("Bearer ", "") not in self.tokens:
            return httpx.Response(
                401, content=b"unauthorized",
                headers={"WWW-Authenticate": (
                    'Bearer resource_metadata="https://mcp.example.com/'
                    '.well-known/oauth-protected-resource/mcp", scope="mcp:tools"'
                )},
            )
        body = json.loads(request.content)
        self.mcp_requests.append({"method": body.get("method"), "auth": auth})
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"protocolVersion": "2025-06-18"}})
        if body.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": [{"name": "t"}]}})
        return httpx.Response(202)

    def opener(self, auth_url: str):
        """Fake browser: validates the authorize request and hits the loopback
        callback with a code bound to the PKCE challenge."""
        q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
        assert auth_url.startswith(f"{AS}/authorize?")
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert q["client_id"] == "cid-123" and q["resource"] == MCP_URL
        assert q["scope"] == "mcp:tools"
        # the AS would verify the challenge against the verifier; we record
        # the challenge and compare S256(verifier) at /token.
        code = "code-xyz"
        self.codes[code] = {"challenge": q["code_challenge"], "verifier": None}

        def _hit():
            time.sleep(0.05)
            urllib.request.urlopen(f"{q['redirect_uri']}?code={code}&state={q['state']}", timeout=5)

        threading.Thread(target=_hit, daemon=True).start()
        return True


def _patch_verifier_check(server: _FakeAuthServer, monkeypatch):
    """Make the fake AS check S256(verifier) == challenge for the issued code."""
    real = oauth._pkce_pair

    def pair():
        v, c = real()
        # store verifier under the next code issued
        server._pending_verifier = v  # type: ignore[attr-defined]
        return v, c

    monkeypatch.setattr(oauth, "_pkce_pair", pair)
    orig_opener = server.opener

    def opener(url):
        r = orig_opener(url)
        import base64
        import hashlib
        v = server._pending_verifier  # type: ignore[attr-defined]
        c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        for code, rec in server.codes.items():
            assert rec["challenge"] == c  # challenge no authorize bate com o verifier
            rec["verifier"] = v
        return r

    return opener


class TestLoginFlow:
    def test_full_flow_persists_token(self, monkeypatch):
        srv = _FakeAuthServer()
        opener = _patch_verifier_check(srv, monkeypatch)
        http = httpx.Client(transport=httpx.MockTransport(srv.handler))
        notes: list[str] = []
        entry = oauth.login(MCP_URL, http, opener=opener, notify=notes.append)
        assert entry["access_token"] == "at-1" and entry["refresh_token"] == "rt-1"
        assert entry["client_id"] == "cid-123"
        assert srv.registered and srv.registered[0]["token_endpoint_auth_method"] == "none"
        assert srv.registered[0]["redirect_uris"][0].startswith("http://127.0.0.1:")
        stored = oauth.get_stored(MCP_URL)
        assert stored["access_token"] == "at-1"
        assert oauth._store_path().stat().st_mode & 0o777 == 0o600
        assert any("Abrindo o navegador" in n for n in notes)
        assert oauth.cached_access_token(MCP_URL) == "at-1"

    def test_discovery_via_www_authenticate(self):
        srv = _FakeAuthServer()
        http = httpx.Client(transport=httpx.MockTransport(srv.handler))
        meta = oauth.discover(
            MCP_URL, http,
            'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp"',
        )
        assert meta["token_endpoint"] == f"{AS}/token"
        assert meta["resource"] == MCP_URL and meta["scopes"] == ["mcp:tools"]
        assert meta["registration_endpoint"] == f"{AS}/register"

    def test_discovery_fails_cleanly(self):
        def nothing(request):
            return httpx.Response(404)

        http = httpx.Client(transport=httpx.MockTransport(nothing))
        with pytest.raises(oauth.OAuthError, match="metadata OAuth"):
            oauth.discover("https://other.example.com/mcp", http)

    def test_no_registration_requires_client_id(self, monkeypatch):
        srv = _FakeAuthServer(with_registration=False)
        http = httpx.Client(transport=httpx.MockTransport(srv.handler))
        with pytest.raises(oauth.OAuthError, match="client_id"):
            oauth.login(MCP_URL, http, opener=lambda u: True, notify=lambda s: None)

    def test_state_mismatch_rejected(self, monkeypatch):
        srv = _FakeAuthServer()
        http = httpx.Client(transport=httpx.MockTransport(srv.handler))

        def bad_opener(auth_url):
            q = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
            threading.Thread(
                target=lambda: urllib.request.urlopen(
                    f"{q['redirect_uri']}?code=x&state=WRONG", timeout=5),
                daemon=True,
            ).start()
            return True

        with pytest.raises(oauth.OAuthError, match="negada"):
            oauth.login(MCP_URL, http, opener=bad_opener, notify=lambda s: None)
        assert oauth.get_stored(MCP_URL) is None

    def test_callback_timeout(self, monkeypatch):
        srv = _FakeAuthServer()
        http = httpx.Client(transport=httpx.MockTransport(srv.handler))
        meta = oauth.discover(MCP_URL, http)
        with pytest.raises(oauth.OAuthError, match="tempo esgotado"):
            oauth.authorize(MCP_URL, meta, http, opener=lambda u: True, timeout=0.2,
                            notify=lambda s: None)


class TestStoreAndRefresh:
    def test_refresh_when_expired(self):
        srv = _FakeAuthServer()
        http = httpx.Client(transport=httpx.MockTransport(srv.handler))
        oauth.put_stored(MCP_URL, {
            "access_token": "old", "refresh_token": "rt-1", "client_id": "cid-123",
            "token_endpoint": f"{AS}/token", "authorization_endpoint": f"{AS}/authorize",
            "resource": MCP_URL, "expires_at": time.time() - 10,
        })
        assert oauth.cached_access_token(MCP_URL, http) == "at-2"
        assert srv.refreshes == 1
        assert oauth.get_stored(MCP_URL)["access_token"] == "at-2"
        assert oauth.get_stored(MCP_URL)["refresh_token"] == "rt-1"  # preservado

    def test_expired_without_http_is_none(self):
        oauth.put_stored(MCP_URL, {"access_token": "old", "expires_at": time.time() - 10})
        assert oauth.cached_access_token(MCP_URL) is None

    def test_forget(self):
        oauth.put_stored(MCP_URL, {"access_token": "x"})
        assert oauth.forget(MCP_URL) is True
        assert oauth.forget(MCP_URL) is False
        assert oauth.get_stored(MCP_URL) is None

    def test_interactive_allowed_gates(self, monkeypatch):
        monkeypatch.setenv("ALPHA_MCP_OAUTH", "0")
        assert oauth.interactive_allowed() is False
        monkeypatch.delenv("ALPHA_MCP_OAUTH")
        monkeypatch.setenv("ALPHA_DELEGATED", "1")
        assert oauth.interactive_allowed() is False


class TestHttpClientIntegration:
    def test_401_without_static_header_raises_auth_required(self):
        srv = _FakeAuthServer()
        client = MCPHttpClient(name="lin", url=MCP_URL, transport=httpx.MockTransport(srv.handler))
        client.start()
        assert client.oauth_enabled is True
        with pytest.raises(MCPAuthRequired) as ei:
            client.initialize()
        assert "/mcp auth lin" in str(ei.value)
        assert "resource_metadata" in (ei.value.www_authenticate or "")

    def test_static_header_disables_oauth(self):
        client = MCPHttpClient(name="x", url=MCP_URL, headers={"Authorization": "Bearer s"})
        assert client.oauth_enabled is False

    def test_stored_token_used_on_start(self):
        srv = _FakeAuthServer()
        srv.tokens.add("at-1")
        oauth.put_stored(MCP_URL, {"access_token": "at-1"})
        client = MCPHttpClient(name="lin", url=MCP_URL, transport=httpx.MockTransport(srv.handler))
        client.start()
        client.initialize()
        assert client.list_tools() == [{"name": "t"}]
        assert all(r["auth"] == "Bearer at-1" for r in srv.mcp_requests)

    def test_expired_stored_token_refreshed_on_start(self):
        srv = _FakeAuthServer()
        oauth.put_stored(MCP_URL, {
            "access_token": "old", "refresh_token": "rt-1", "client_id": "cid-123",
            "token_endpoint": f"{AS}/token", "resource": MCP_URL,
            "expires_at": time.time() - 10,
        })
        client = MCPHttpClient(name="lin", url=MCP_URL, transport=httpx.MockTransport(srv.handler))
        client.start()
        client.initialize()
        assert srv.mcp_requests[0]["auth"] == "Bearer at-2"


class TestCommand:
    def _ctx(self):
        class _Ctx:
            pass
        return _Ctx()

    def test_auth_runs_login_and_reconnects(self, monkeypatch, capsys):
        from alpha.cli.commands import agent_meta
        from alpha.mcp import loader
        from alpha.mcp.config import MCPServerConfig

        spec = MCPServerConfig(name="lin", url=MCP_URL)
        monkeypatch.setattr(loader, "find_spec", lambda n: spec if n == "lin" else None)
        monkeypatch.setattr(oauth, "interactive_allowed", lambda: True)
        calls = {}
        monkeypatch.setattr(oauth, "login", lambda url, **kw: calls.setdefault("login", (url, kw)))

        class _C:
            tools = [{"name": "a"}, {"name": "b"}]

        monkeypatch.setattr(loader, "connect_server", lambda n: calls.setdefault("connect", n) and _C())
        agent_meta._handle_mcp(self._ctx(), ["/mcp", "auth", "lin"])
        out = capsys.readouterr().out
        assert calls["login"][0] == MCP_URL and calls["connect"] == "lin"
        assert "autorizado" in out and "2 tool(s)" in out

    def test_logout_and_errors(self, monkeypatch, capsys):
        from alpha.cli.commands import agent_meta
        from alpha.mcp import loader
        from alpha.mcp.config import MCPServerConfig

        spec = MCPServerConfig(name="lin", url=MCP_URL)
        monkeypatch.setattr(loader, "find_spec", lambda n: spec if n == "lin" else None)
        oauth.put_stored(MCP_URL, {"access_token": "x"})
        agent_meta._handle_mcp(self._ctx(), ["/mcp", "logout", "lin"])
        assert "removido" in capsys.readouterr().out
        assert oauth.get_stored(MCP_URL) is None

        agent_meta._handle_mcp(self._ctx(), ["/mcp", "auth"])
        assert "Usage" in capsys.readouterr().out
        monkeypatch.setattr(agent_meta, "print_error", lambda m: print("ERR " + m))
        agent_meta._handle_mcp(self._ctx(), ["/mcp", "auth", "ghost"])
        assert "ERR" in capsys.readouterr().out
        monkeypatch.setattr(oauth, "interactive_allowed", lambda: False)
        agent_meta._handle_mcp(self._ctx(), ["/mcp", "auth", "lin"])
        assert "indisponível" in capsys.readouterr().out


def test_config_parses_client_id_and_auth(tmp_path, monkeypatch):
    from alpha.mcp.config import load_mcp_config

    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {
        "a": {"url": "https://a.example.com/mcp", "client_id": "cid", "auth": "NONE"},
    }}))
    monkeypatch.setattr("alpha.mcp.config.is_user_scoped_config", lambda path: True)
    specs = load_mcp_config(p)
    assert specs[0].client_id == "cid" and specs[0].auth == "none"
