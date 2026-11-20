"""Testes do transporte MCP Streamable HTTP (alpha/mcp/http_client.py)
e do parsing de entradas HTTP no mcp.json.

Usa httpx.MockTransport — nenhuma rede real.
"""

from __future__ import annotations

import json

import httpx
import pytest

import alpha.mcp.http_client as http_client_mod
from alpha.mcp.client import MCPError
from alpha.mcp.config import load_mcp_config
from alpha.mcp.http_client import MCPHttpClient, validate_mcp_url

URL = "https://mcp.example.com/mcp"


# ── validate_mcp_url ─────────────────────────────────────────────


class TestValidateUrl:
    def test_https_ok(self, monkeypatch):
        # SSRF guard (#D304) does real DNS; offline we simulate a public host.
        monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)
        assert validate_mcp_url("https://mcp.example.com/mcp") is None

    def test_ssrf_private_ip_blocked(self):
        # #D304: IP literals resolve without DNS, so this is offline-safe.
        for u in (
            "https://169.254.169.254/latest/meta-data/",  # cloud metadata
            "https://10.0.0.5/mcp",
            "https://192.168.1.1/mcp",
        ):
            err = validate_mcp_url(u)
            assert err is not None and "SSRF" in err

    def test_http_localhost_ok(self):
        assert validate_mcp_url("http://localhost:8080/mcp") is None
        assert validate_mcp_url("http://127.0.0.1:8080/mcp") is None

    def test_http_remote_rejected(self):
        err = validate_mcp_url("http://mcp.example.com/mcp")
        assert err is not None and "https" in err

    def test_bad_scheme_rejected(self):
        assert validate_mcp_url("ftp://x/mcp") is not None

    def test_missing_host_rejected(self):
        assert validate_mcp_url("https:///mcp") is not None


# ── Client protocol ──────────────────────────────────────────────


def _rpc_response(req: dict, result: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": req.get("id"), "result": result},
        headers=headers or {},
    )


def _make_client(handler) -> MCPHttpClient:
    client = MCPHttpClient(
        name="test",
        url=URL,
        headers={"Authorization": "Bearer tok-123"},
        transport=httpx.MockTransport(handler),
    )
    client.start()
    return client


class TestHandshake:
    @pytest.fixture(autouse=True)
    def _public_host(self, monkeypatch):
        # The mock transport uses mcp.example.com; the #D304 SSRF guard does real
        # DNS (unavailable offline) — treat the mock host as public here.
        monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)

    def test_initialize_captures_session_and_notifies(self):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen.append({"body": body, "headers": dict(request.headers)})
            if body.get("method") == "initialize":
                return _rpc_response(
                    body,
                    {"protocolVersion": "2025-06-18"},
                    headers={"Mcp-Session-Id": "sess-42"},
                )
            return httpx.Response(202)

        client = _make_client(handler)
        result = client.initialize()

        assert result["protocolVersion"] == "2025-06-18"
        assert client._session_id == "sess-42"
        # initialize + notifications/initialized
        assert [s["body"].get("method") for s in seen] == [
            "initialize", "notifications/initialized",
        ]
        # Auth header em todas as requests; session id na notification
        assert seen[0]["headers"]["authorization"] == "Bearer tok-123"
        assert seen[1]["headers"]["mcp-session-id"] == "sess-42"
        assert seen[1]["headers"]["mcp-protocol-version"] == "2025-06-18"

    def test_list_tools_and_call_tool_json(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            method = body.get("method")
            if method == "tools/list":
                return _rpc_response(body, {"tools": [
                    {"name": "search", "description": "d", "inputSchema": {}},
                ]})
            if method == "tools/call":
                assert body["params"] == {"name": "search", "arguments": {"q": "x"}}
                return _rpc_response(body, {"content": [{"type": "text", "text": "achei"}]})
            return httpx.Response(202)

        client = _make_client(handler)
        tools = client.list_tools()
        assert tools[0]["name"] == "search"
        raw = client.call_tool("search", {"q": "x"})
        assert raw["content"][0]["text"] == "achei"

    def test_sse_response_with_interleaved_notification(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sse = (
                'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n'
                "\n"
                f'data: {{"jsonrpc":"2.0","id":{body["id"]},'
                f'"result":{{"content":[{{"type":"text","text":"via sse"}}]}}}}\n'
                "\n"
            )
            return httpx.Response(
                200, content=sse.encode(),
                headers={"content-type": "text/event-stream"},
            )

        client = _make_client(handler)
        raw = client.call_tool("t", {})
        assert raw["content"][0]["text"] == "via sse"
        assert len(client._notifications) == 1

    def test_jsonrpc_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body.get("id"),
                "error": {"code": -32601, "message": "no such method"},
            })

        client = _make_client(handler)
        with pytest.raises(MCPError, match="no such method"):
            client.list_tools()

    def test_401_raises_auth_hint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=b"unauthorized")

        client = _make_client(handler)
        with pytest.raises(MCPError, match="autentica"):
            client.list_tools()

    def test_http_url_remote_refused_at_start(self):
        client = MCPHttpClient(name="bad", url="http://remoto.example.com/mcp")
        with pytest.raises(MCPError, match="https"):
            client.start()

    def test_oversized_response_rejected(self, monkeypatch):
        monkeypatch.setattr(http_client_mod, "_MAX_RESPONSE_BYTES", 64)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return _rpc_response(body, {"pad": "x" * 500})

        client = _make_client(handler)
        with pytest.raises(MCPError, match="exceeded"):
            client.list_tools()

    def test_stop_sends_session_delete(self):
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.method == "DELETE":
                return httpx.Response(200)
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return _rpc_response(body, {}, headers={"Mcp-Session-Id": "s1"})
            return httpx.Response(202)

        client = _make_client(handler)
        client.initialize()
        client.stop()
        assert "DELETE" in methods
        assert client._client is None


# ── Config parsing ───────────────────────────────────────────────


class TestHttpConfig:
    @pytest.fixture(autouse=True)
    def _trust_project_mcp(self, monkeypatch):
        # These parse servers from explicit project-scoped paths; opt in past
        # the #D305 fail-closed gate (the gate itself lives in
        # test_project_config_trust.py). HTTP entries are unaffected either way.
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")

    def _load(self, tmp_path, payload: dict):
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps(payload))
        return load_mcp_config(cfg)

    def test_http_entry_parsed_with_header_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LINEAR_MCP_TOKEN", "tok-abc")
        specs = self._load(tmp_path, {"mcpServers": {"linear": {
            "type": "http",
            "url": "https://mcp.linear.app/mcp",
            "headers": {"Authorization": "Bearer ${LINEAR_MCP_TOKEN}"},
        }}})
        assert len(specs) == 1
        spec = specs[0]
        assert spec.is_http
        assert spec.url == "https://mcp.linear.app/mcp"
        assert spec.headers["Authorization"] == "Bearer tok-abc"

    def test_secret_var_in_url_refused(self, tmp_path):
        specs = self._load(tmp_path, {"mcpServers": {"bad": {
            "url": "https://x.example.com/mcp?key=${MY_API_KEY}",
        }}})
        assert specs == []

    def test_type_http_without_url_skipped(self, tmp_path):
        specs = self._load(tmp_path, {"mcpServers": {"bad": {"type": "http"}}})
        assert specs == []

    def test_stdio_entry_still_works(self, tmp_path):
        specs = self._load(tmp_path, {"mcpServers": {"fs": {
            "command": "npx", "args": ["-y", "server-fs"],
        }}})
        assert len(specs) == 1
        assert not specs[0].is_http
        assert specs[0].command == "npx"

    def test_disabled_http_entry(self, tmp_path):
        specs = self._load(tmp_path, {"mcpServers": {"off": {
            "url": "https://x.example.com/mcp", "disabled": True,
        }}})
        assert specs[0].disabled is True


# ── Loader branch ────────────────────────────────────────────────


class TestLoaderBranch:
    def test_connect_one_uses_http_client_for_url(self, monkeypatch):
        from alpha.mcp import loader
        from alpha.mcp.config import MCPServerConfig

        created = {}

        class FakeHttp:
            def __init__(self, name, url, headers, oauth=None):
                created.update({"name": name, "url": url, "headers": headers, "oauth": oauth})
                self.name = name
                self.tools = []

            def start(self):
                pass

            def initialize(self):
                return {}

            def list_tools(self):
                return []

        monkeypatch.setattr(loader, "MCPHttpClient", FakeHttp)
        spec = MCPServerConfig(
            name="remote", url="https://x.example.com/mcp",
            headers={"Authorization": "Bearer t"},
        )
        client = loader._connect_one(spec, startup_timeout=5)
        assert client is not None
        assert created["url"] == "https://x.example.com/mcp"
        assert created["headers"]["Authorization"] == "Bearer t"


# ── AUDIT_V2.3 #302/#304/#104/#500 ───────────────────────────────


class TestSseFloodCaps:
    @pytest.fixture(autouse=True)
    def _public_host(self, monkeypatch):
        monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)

    def test_blank_line_flood_hits_line_cap(self, monkeypatch):
        """#302: fluxo contínuo de linhas em branco (delimitador SSE sem
        evento) não pode rodar para sempre — cap de linhas encerra."""
        monkeypatch.setattr(http_client_mod, "_MAX_SSE_LINES", 1000)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"\n" * 5000,
            )

        client = _make_client(handler)
        with pytest.raises(MCPError, match="lines"):
            client._request("tools/list")

    def test_sse_deadline_enforced(self, monkeypatch):
        """#302: teto de tempo total do stream via time.monotonic()."""
        monkeypatch.setattr(http_client_mod, "_SSE_TOTAL_DEADLINE", 0.0)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: {}\n\n" * 10,
            )

        client = _make_client(handler)
        with pytest.raises(MCPError, match="exceeded"):
            client._request("tools/list")


class TestStopDeleteTimeout:
    def test_stop_delete_uses_short_timeout(self, monkeypatch):
        """#304: o DELETE de sessão no shutdown leva timeout explícito curto
        (3s) em vez de herdar os 30s do client."""
        monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                seen["timeout"] = request.extensions.get("timeout")
                return httpx.Response(200)
            return _rpc_response(
                json.loads(request.content), {}, headers={"mcp-session-id": "s1"}
            )

        client = _make_client(handler)
        client._request("initialize")  # captura session id
        assert client._session_id == "s1"
        client.stop()
        assert seen, "DELETE não foi enviado"
        assert seen["timeout"]["read"] == 3.0
        assert seen["timeout"]["connect"] == 3.0


class TestPrivateAllowOptIn:
    def test_private_ip_allowed_with_env(self, monkeypatch):
        """#104 (nota da review): ALPHA_MCP_ALLOW_PRIVATE=1 libera MCP
        interno legítimo sobre https."""
        monkeypatch.setenv("ALPHA_MCP_ALLOW_PRIVATE", "1")
        assert validate_mcp_url("https://10.0.0.5/mcp") is None

    def test_private_ip_blocked_by_default(self, monkeypatch):
        monkeypatch.delenv("ALPHA_MCP_ALLOW_PRIVATE", raising=False)
        err = validate_mcp_url("https://10.0.0.5/mcp")
        assert err is not None and "SSRF" in err


class TestProtocolDedupe:
    def test_clients_share_protocol_methods(self):
        """#500: initialize/list_tools/call_tool vêm do mixin único — não
        há mais cópias divergentes entre stdio e HTTP."""
        from alpha.mcp.client import MCPClient, _MCPProtocolMixin

        for meth in ("initialize", "list_tools", "call_tool"):
            assert getattr(MCPClient, meth) is getattr(_MCPProtocolMixin, meth)
            assert getattr(MCPHttpClient, meth) is getattr(_MCPProtocolMixin, meth)

    def test_client_info_uses_package_version(self):
        from alpha.mcp.client import _MCPProtocolMixin, _package_version

        info = _MCPProtocolMixin._CLIENT_INFO
        assert info["name"] == "alpha-code"
        assert info["version"] == _package_version()
        assert info["version"] != "1.0.0"  # o hardcode stale do #500
