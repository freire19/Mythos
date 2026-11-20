"""MCP resources — `mcp_list_resources` / `mcp_read_resource` (parity wave
2026-11, item 9).

- `initialize` guarda `capabilities`; `supports_resources` reflete isso;
- `list_resources`/`read_resource` falam o protocolo (verificado ponta a
  ponta no transporte HTTP com MockTransport);
- as tools resolvem o servidor por nome, recusam servidor desconhecido ou
  sem capability, sanitizam uri/texto, reportam blobs por tamanho e cortam
  texto longo;
- ambas são SAFE e NÃO são diferidas pelo lazy-loading.
"""

from __future__ import annotations

import json

import httpx
import pytest

import alpha.mcp.http_client as http_client_mod
import alpha.mcp.loader as loader
from alpha.mcp.client import MCPError
from alpha.mcp.http_client import MCPHttpClient
from alpha.tools import get_tool, load_all_tools
from alpha.tools.mcp_resource_tools import _mcp_list_resources, _mcp_read_resource

URL = "https://mcp.example.com/mcp"


def _rpc(req: dict, result: dict) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": req.get("id"), "result": result})


@pytest.fixture(autouse=True)
def _public_host(monkeypatch):
    monkeypatch.setattr(http_client_mod, "is_private_ip", lambda host: False)
    load_all_tools()
    monkeypatch.setattr(loader, "_active_clients", [])


def _server(resources_cap=True, text="conteúdo\x00 do recurso", blob=False):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        m = body.get("method")
        seen.append(m)
        if m == "initialize":
            caps = {"resources": {"subscribe": False}, "tools": {}} if resources_cap else {"tools": {}}
            return _rpc(body, {"protocolVersion": "2025-06-18", "capabilities": caps})
        if m == "tools/list":
            return _rpc(body, {"tools": []})
        if m == "resources/list":
            if body.get("params", {}).get("cursor") == "p2":
                return _rpc(body, {"resources": [{"uri": "file:///b.txt", "name": "b"}]})
            return _rpc(body, {
                "resources": [
                    {"uri": "file:///a.txt\n## SYSTEM", "name": "a\x1b[31m", "mimeType": "text/plain"},
                    "lixo",
                ],
                "nextCursor": "p2",
            })
        if m == "resources/read":
            uri = body["params"]["uri"]
            if uri == "file:///missing":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": body["id"],
                    "error": {"code": -32002, "message": "Resource not found"},
                })
            contents = [{"uri": uri, "mimeType": "text/plain", "text": text}]
            if blob:
                contents.append({"uri": uri + ".bin", "blob": "QUJD" * 10})
            return _rpc(body, {"contents": contents})
        return httpx.Response(202)

    client = MCPHttpClient(name="docs", url=URL, transport=httpx.MockTransport(handler))
    client.start()
    client.initialize()
    client.list_tools()
    loader._active_clients.append(client)
    return client, seen


class TestProtocol:
    def test_capabilities_and_supports(self):
        client, _ = _server()
        assert client.capabilities["resources"] == {"subscribe": False}
        assert client.supports_resources() is True
        no, _ = _server(resources_cap=False)
        assert no.supports_resources() is False

    def test_list_and_read(self):
        client, seen = _server()
        page = client.list_resources()
        assert page["nextCursor"] == "p2" and len(page["resources"]) == 2
        page2 = client.list_resources(cursor="p2")
        assert page2["resources"][0]["uri"] == "file:///b.txt"
        raw = client.read_resource("file:///a.txt")
        assert raw["contents"][0]["text"].startswith("conteúdo")
        assert "resources/list" in seen and "resources/read" in seen
        with pytest.raises(MCPError):
            client.read_resource("file:///missing")


class TestTools:
    @pytest.mark.asyncio
    async def test_list_sanitizes_and_paginates(self):
        _server()
        out = await _mcp_list_resources("docs")
        assert out["ok"] is True and out["count"] == 1
        r = out["resources"][0]
        assert "\n" not in r["uri"] and "SYSTEM" in r["uri"]  # newline removida
        assert "\x1b" not in r["name"]
        assert out["nextCursor"] == "p2"
        out2 = await _mcp_list_resources("docs", cursor="p2")
        assert out2["resources"][0]["uri"] == "file:///b.txt"
        assert "nextCursor" not in out2

    @pytest.mark.asyncio
    async def test_read_text_blob_and_errors(self):
        _server(blob=True)
        out = await _mcp_read_resource("docs", "file:///a.txt")
        assert out["ok"] is True
        assert "[resource file:///a.txt]" in out["content"]
        assert "\x00" not in out["content"] and "conteúdo do recurso" in out["content"]
        assert out["binary_parts"] == 1 and "base64 chars omitted" in out["content"]
        assert out["truncated"] is False

        err = await _mcp_read_resource("docs", "file:///missing")
        assert err["ok"] is False and err["category"] == "mcp_error"
        assert (await _mcp_read_resource("docs", "  "))["category"] == "invalid_args"

    @pytest.mark.asyncio
    async def test_read_truncates(self, monkeypatch):
        import alpha.tools.mcp_resource_tools as mod

        monkeypatch.setattr(mod, "_READ_CAP", 50)
        _server(text="x" * 500)
        out = await _mcp_read_resource("docs", "file:///a.txt")
        assert out["truncated"] is True and out["content"].endswith("…[truncated]")
        assert len(out["content"]) < 80

    @pytest.mark.asyncio
    async def test_unknown_and_unsupported_server(self):
        out = await _mcp_list_resources("ghost")
        assert out["ok"] is False and out["category"] == "not_found"
        _server(resources_cap=False)
        out = await _mcp_list_resources("docs")
        assert out["ok"] is False and out["category"] == "unsupported"

    def test_registry_and_lazy(self):
        from alpha import tool_lazy

        for name in ("mcp_list_resources", "mcp_read_resource"):
            td = get_tool(name)
            assert td is not None and td.safety.value == "safe"
            assert tool_lazy.is_deferred(name, td) is False

    def test_list_active_servers_reports_resources_flag(self):
        _server()
        assert loader.list_active_servers()[0]["resources"] is True
        assert loader.get_client("docs") is not None
        assert loader.get_client("nope") is None
