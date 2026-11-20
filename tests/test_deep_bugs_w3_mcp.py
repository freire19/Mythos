"""Testes das issues #DB14 e #DB24 (DEEP_BUGS wave 3 — MCP).

#DB14: `headers`/`env` não-dict num servidor não pode abortar o load de
todos os servidores do mcp.json.
#DB24: id JSON-RPC ecoado como string ("1" vs 1) precisa casar com o
request pendente nos dois transportes (stdio e Streamable HTTP).
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time

import httpx

from alpha.mcp.client import MCPClient
from alpha.mcp.config import load_mcp_config
from alpha.mcp.http_client import MCPHttpClient

URL = "https://mcp.example.com/mcp"


# ── #DB14: headers/env malformados ───────────────────────────────


class TestDB14MalformedHeadersEnv:
    def _write_config(self, tmp_path, servers: dict):
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": servers}))
        return cfg

    def test_headers_nao_dict_nao_aborta_outros_servidores(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        cfg = self._write_config(
            tmp_path,
            {
                "quebrado": {"url": URL, "headers": ["Authorization: Bearer x"]},
                "bom": {"url": URL, "headers": {"X-Ok": "1"}},
            },
        )
        servers = load_mcp_config(cfg)
        names = {s.name for s in servers}
        assert names == {"quebrado", "bom"}
        by_name = {s.name: s for s in servers}
        assert by_name["quebrado"].headers == {}
        assert by_name["bom"].headers == {"X-Ok": "1"}

    def test_headers_string_vira_dict_vazio(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        cfg = self._write_config(
            tmp_path, {"s": {"url": URL, "headers": "Bearer x"}}
        )
        servers = load_mcp_config(cfg)
        assert len(servers) == 1
        assert servers[0].headers == {}

    def test_env_nao_dict_nao_aborta_outros_servidores(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        cfg = self._write_config(
            tmp_path,
            {
                "quebrado": {"command": "echo", "env": ["FOO=bar"]},
                "bom": {"command": "echo", "env": {"FOO": "bar"}},
            },
        )
        servers = load_mcp_config(cfg)
        names = {s.name for s in servers}
        assert names == {"quebrado", "bom"}
        by_name = {s.name: s for s in servers}
        assert by_name["quebrado"].env == {}
        assert by_name["bom"].env == {"FOO": "bar"}

    def test_env_string_vira_dict_vazio(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        cfg = self._write_config(
            tmp_path, {"s": {"command": "echo", "env": "FOO=bar"}}
        )
        servers = load_mcp_config(cfg)
        assert len(servers) == 1
        assert servers[0].env == {}

    def test_headers_env_dict_seguem_funcionando(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        monkeypatch.setenv("MEU_VALOR", "abc")
        cfg = self._write_config(
            tmp_path,
            {
                "h": {"url": URL, "headers": {"X-Tok": "${MEU_VALOR}"}},
                "e": {"command": "echo", "env": {"FOO": "${MEU_VALOR}"}},
            },
        )
        by_name = {s.name: s for s in load_mcp_config(cfg)}
        assert by_name["h"].headers == {"X-Tok": "abc"}
        assert by_name["e"].env == {"FOO": "abc"}


# ── #DB24: id JSON-RPC string vs int ─────────────────────────────


class _FakeProc:
    """Só o que _read_stdout usa: um stdout iterável por linhas."""

    def __init__(self, lines: str):
        self.stdout = io.StringIO(lines)


class TestDB24StdioIdMismatch:
    def test_reader_roteia_resposta_com_id_string(self):
        client = MCPClient("t", "true")
        pq: queue.Queue = queue.Queue()
        client._pending["7"] = pq
        client.proc = _FakeProc(
            '{"jsonrpc": "2.0", "id": "7", "result": {"ok": 1}}\n'
        )
        client._read_stdout()
        msg = pq.get_nowait()
        assert msg == {"jsonrpc": "2.0", "id": "7", "result": {"ok": 1}}
        # EOF → sentinela de desconexão continua sendo enfileirado.
        assert pq.get_nowait() is None

    def test_request_aceita_resposta_com_id_string(self):
        client = MCPClient("t", "true")

        def fake_send(message: dict) -> None:
            req_id = message["id"]

            def deliver() -> None:
                for _ in range(200):
                    with client._pending_lock:
                        pq = client._pending.get(str(req_id))
                    if pq is not None:
                        pq.put(
                            {
                                "jsonrpc": "2.0",
                                "id": str(req_id),
                                "result": {"ok": 2},
                            }
                        )
                        return
                    time.sleep(0.01)

            threading.Thread(target=deliver, daemon=True).start()

        client._send = fake_send  # type: ignore[method-assign]
        result = client._request("x", timeout=5)
        assert result == {"ok": 2}
        assert client._pending == {}


class TestDB24HttpIdMismatch:
    def _client(self, handler) -> MCPHttpClient:
        client = MCPHttpClient(
            "t", "http://localhost/mcp", transport=httpx.MockTransport(handler)
        )
        client.start()
        return client

    def test_json_response_com_id_string_casa(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": str(body["id"]),
                    "result": {"tools": []},
                },
            )

        client = self._client(handler)
        try:
            assert client._request("tools/list") == {"tools": []}
        finally:
            client.stop()

    def test_sse_response_com_id_string_casa(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            msg = json.dumps(
                {"jsonrpc": "2.0", "id": str(body["id"]), "result": {"ok": 3}}
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {msg}\n\n".encode(),
            )

        client = self._client(handler)
        try:
            assert client._request("tools/list") == {"ok": 3}
        finally:
            client.stop()
