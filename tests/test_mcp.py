"""Tests for the MCP client + loader.

The end-to-end test spawns a tiny stub MCP server (this same Python file run
with `--server`) so we exercise the real subprocess + JSON-RPC path without
adding a network or external-binary dependency.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from alpha.mcp.client import MCPClient, MCPError
from alpha.mcp.config import MCPServerConfig, load_mcp_config
from alpha.mcp.loader import _format_tool_result


# ── Stub server ──

STUB_SERVER = textwrap.dedent(
    """
    import json, sys

    def reply(req_id, result):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\\n")
        sys.stdout.flush()

    def err(req_id, code, msg):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            reply(req_id, {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "stub"}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            reply(req_id, {"tools": [
                {"name": "echo", "description": "Echo back text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
                {"name": "boom", "description": "Always errors", "inputSchema": {"type": "object", "properties": {}}},
            ]})
        elif method == "tools/call":
            params = msg.get("params", {})
            tool = params.get("name")
            args = params.get("arguments", {})
            if tool == "echo":
                reply(req_id, {"content": [{"type": "text", "text": args.get("text", "")}], "isError": False})
            elif tool == "boom":
                reply(req_id, {"content": [{"type": "text", "text": "kaboom"}], "isError": True})
            else:
                err(req_id, -32601, f"unknown tool: {tool}")
        else:
            err(req_id, -32601, f"unknown method: {method}")
    """
).strip()


@pytest.fixture
def stub_server_path(tmp_path: Path) -> Path:
    p = tmp_path / "stub_mcp_server.py"
    p.write_text(STUB_SERVER, encoding="utf-8")
    return p


@pytest.fixture
def mcp_client(stub_server_path: Path):
    client = MCPClient(name="stub", command=sys.executable, args=[str(stub_server_path)])
    client.start()
    try:
        yield client
    finally:
        client.stop()


# ── Client / protocol ──


class TestMCPClient:
    def test_initialize_and_list_tools(self, mcp_client: MCPClient):
        mcp_client.initialize()
        tools = mcp_client.list_tools()
        names = [t["name"] for t in tools]
        assert names == ["echo", "boom"]

    def test_call_tool_success(self, mcp_client: MCPClient):
        mcp_client.initialize()
        mcp_client.list_tools()
        result = mcp_client.call_tool("echo", {"text": "hello"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == "hello"

    def test_call_tool_isError_propagates(self, mcp_client: MCPClient):
        mcp_client.initialize()
        result = mcp_client.call_tool("boom", {})
        assert result["isError"] is True

    def test_unknown_tool_returns_jsonrpc_error(self, mcp_client: MCPClient):
        mcp_client.initialize()
        with pytest.raises(MCPError, match="unknown tool"):
            mcp_client.call_tool("does_not_exist", {})


class TestMissingBinary:
    def test_start_raises_when_command_missing(self):
        client = MCPClient(name="ghost", command="/nonexistent/binary", args=[])
        with pytest.raises(MCPError, match="Cannot spawn"):
            client.start()


# ── AUDIT_V1.8 #034 — startup timeout ──

class TestConnectOneTimeout:
    """Garante que _connect_one nao trava indefinidamente em server lento.

    Server stub que dorme infinito (sem responder JSON-RPC) deve ser morto
    no `startup_timeout` em vez de pendurar o CLI todo.
    """

    def test_hung_server_killed_within_timeout(self, tmp_path: Path):
        from alpha.mcp.loader import _connect_one
        import time

        # Stub que abre stdin mas nunca responde — simula Popen lento ou
        # subprocess preso em busy-loop antes do JSON-RPC handshake.
        stub = tmp_path / "hang_server.py"
        stub.write_text(
            "import sys, time\n"
            "for line in sys.stdin:\n"
            "    pass\n"  # absorve mas nunca responde
            "time.sleep(999)\n"
        )

        spec = MCPServerConfig(
            name="hung",
            command=sys.executable,
            args=[str(stub)],
        )

        t0 = time.monotonic()
        result = _connect_one(spec, startup_timeout=1.0)
        elapsed = time.monotonic() - t0

        # Deve retornar None (falhou) — e dentro do timeout + margem
        assert result is None
        # Margem generosa para overhead de subprocess + thread join
        assert elapsed < 5.0, f"_connect_one demorou {elapsed:.2f}s, esperado < 5s"


# ── Result formatting ──


class TestFormatToolResult:
    def test_text_content_concatenated(self):
        raw = {"content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "ya"}], "isError": False}
        assert _format_tool_result(raw) == {"output": "hi\nya"}

    def test_error_returns_error_key(self):
        raw = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        assert _format_tool_result(raw) == {"error": "boom"}

    def test_empty_output(self):
        assert _format_tool_result({"content": [], "isError": False}) == {"output": ""}

    def test_non_dict_input(self):
        assert _format_tool_result("oops") == {"output": "oops"}


# ── Config loader ──


class TestConfigLoader:
    @pytest.fixture(autouse=True)
    def _trust_project_mcp(self, monkeypatch):
        # These tests parse stdio servers from explicit project-scoped paths.
        # #D305 makes project stdio servers fail-closed by default; opt in here
        # so parsing is exercised (the gate itself is covered by
        # test_project_config_trust.py).
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")

    def test_no_file_returns_empty(self, tmp_path: Path):
        assert load_mcp_config(tmp_path / "missing.json") == []

    def test_invalid_json_returns_empty(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert load_mcp_config(p) == []

    def test_parses_servers(self, tmp_path: Path):
        p = tmp_path / "mcp.json"
        p.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fs": {"command": "echo", "args": ["a", "b"], "env": {"K": "v"}},
                        "off": {"command": "true", "disabled": True},
                    }
                }
            ),
            encoding="utf-8",
        )
        servers = load_mcp_config(p)
        assert len(servers) == 2
        fs = next(s for s in servers if s.name == "fs")
        assert fs.command == "echo"
        assert fs.args == ["a", "b"]
        assert fs.env == {"K": "v"}
        assert fs.disabled is False
        off = next(s for s in servers if s.name == "off")
        assert off.disabled is True

    def test_skips_entries_without_command(self, tmp_path: Path):
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps({"mcpServers": {"bad": {"args": []}}}), encoding="utf-8")
        assert load_mcp_config(p) == []

    def test_env_var_expansion(self, tmp_path: Path, monkeypatch):
        # Non-secret env var expands normally in both args and env.
        monkeypatch.setenv("MY_DIR", "/data")
        p = tmp_path / "mcp.json"
        p.write_text(
            json.dumps({"mcpServers": {"x": {"command": "cmd", "args": ["${MY_DIR}/db"], "env": {"D": "${MY_DIR}"}}}}),
            encoding="utf-8",
        )
        servers = load_mcp_config(p)
        assert servers[0].args == ["/data/db"]
        assert servers[0].env == {"D": "/data"}

    def test_secret_named_var_in_args_refuses_server(self, tmp_path: Path, monkeypatch):
        # #013: a secret-named env var referenced in `args` would leak to
        # /proc/PID/cmdline → server is NOT started. The secret belongs in env.
        monkeypatch.setenv("MY_TOKEN", "secret123")
        p = tmp_path / "mcp.json"
        p.write_text(
            json.dumps({"mcpServers": {"x": {"command": "cmd", "args": ["${MY_TOKEN}"]}}}),
            encoding="utf-8",
        )
        assert load_mcp_config(p) == []

    def test_secret_named_var_in_env_is_fine(self, tmp_path: Path, monkeypatch):
        # env is the CORRECT place for secrets — server starts, value expanded.
        monkeypatch.setenv("MY_TOKEN", "secret123")
        p = tmp_path / "mcp.json"
        p.write_text(
            json.dumps({"mcpServers": {"x": {"command": "cmd", "env": {"T": "${MY_TOKEN}"}}}}),
            encoding="utf-8",
        )
        servers = load_mcp_config(p)
        assert servers[0].env == {"T": "secret123"}

    def test_literal_token_arg_not_refused(self, tmp_path: Path):
        # #013: keying on the VAR NAME (not the value) — a literal arg merely
        # containing "token" must NOT trip the refusal.
        p = tmp_path / "mcp.json"
        p.write_text(
            json.dumps({"mcpServers": {"x": {"command": "cmd", "args": ["tokens.json", "--key=local"]}}}),
            encoding="utf-8",
        )
        servers = load_mcp_config(p)
        assert servers[0].args == ["tokens.json", "--key=local"]

    def test_unknown_env_var_falls_through(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DEFINITELY_NOT_SET_xyz", raising=False)
        p = tmp_path / "mcp.json"
        p.write_text(
            json.dumps({"mcpServers": {"x": {"command": "cmd", "args": ["${DEFINITELY_NOT_SET_xyz}"]}}}),
            encoding="utf-8",
        )
        servers = load_mcp_config(p)
        assert servers[0].args == ["${DEFINITELY_NOT_SET_xyz}"]


class TestReaderThreadDeath:
    """AUDIT_V2.3 #301: se a iteração de leitura do stdout levantar, a
    thread precisa (a) logar, (b) SEMPRE enfileirar o sentinela `None`
    nos requests pendentes — senão cada request em voo espera o timeout
    inteiro e o client vira buraco negro silencioso."""

    def _client_with_exploding_stdout(self, exc: Exception) -> MCPClient:
        class _ExplodingStdout:
            def __iter__(self):
                yield '{"jsonrpc": "2.0", "id": 99, "result": {}}\n'
                raise exc

        class _FakeProc:
            stdout = _ExplodingStdout()
            stderr = None

        client = MCPClient(name="boom", command="true")
        client.proc = _FakeProc()  # type: ignore[assignment]
        return client

    def test_pending_requests_get_sentinel_on_reader_crash(self):
        import queue as _queue

        client = self._client_with_exploding_stdout(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        )
        pq: _queue.Queue = _queue.Queue()
        with client._pending_lock:
            client._pending[7] = pq

        client._read_stdout()  # não pode propagar a exceção

        # id=99 é órfão (descartado); o pendente id=7 recebe o sentinela.
        assert pq.get_nowait() is None

    def test_stderr_reader_never_raises(self):
        class _ExplodingStderr:
            def __iter__(self):
                raise OSError("read error")

        class _FakeProc:
            stdout = None
            stderr = _ExplodingStderr()

        client = MCPClient(name="boom", command="true")
        client.proc = _FakeProc()  # type: ignore[assignment]
        client._read_stderr()  # não pode propagar

    def test_popen_uses_errors_replace(self):
        """Byte inválido não pode matar a thread com UnicodeDecodeError —
        o Popen decodifica com errors='replace'."""
        import inspect

        from alpha.mcp import client as client_mod

        src = inspect.getsource(client_mod.MCPClient.start)
        assert 'errors="replace"' in src


class TestMakeExecutor:
    """AUDIT_V2.3 #603: o wrapper `_make_executor` registrado como
    `mcp__<server>__<tool>` — clamp de timeout ao turn budget, tradução de
    erros e formatação de sucesso."""

    class _FakeMcpClient:
        name = "fake"

        def __init__(self, behavior):
            self._behavior = behavior
            self.seen: list[tuple] = []

        def call_tool(self, tool_name, arguments, timeout=None):
            self.seen.append((tool_name, arguments, timeout))
            b = self._behavior
            if isinstance(b, Exception):
                raise b
            return b

    def _executor_for(self, behavior):
        from alpha.mcp.loader import _make_executor

        client = self._FakeMcpClient(behavior)
        return client, _make_executor(client, "minha_tool")

    @pytest.mark.asyncio
    async def test_success_formats_tool_result(self, monkeypatch):
        monkeypatch.setattr("alpha._turn_budget.remaining", lambda: None)
        client, ex = self._executor_for(
            {"content": [{"type": "text", "text": "resultado ok"}]}
        )
        out = await ex(x=1)
        assert out == {"output": "resultado ok"}
        assert client.seen[0][0] == "minha_tool"
        assert client.seen[0][1] == {"x": 1}

    @pytest.mark.asyncio
    async def test_mcp_error_becomes_error_dict(self, monkeypatch):
        monkeypatch.setattr("alpha._turn_budget.remaining", lambda: None)
        _, ex = self._executor_for(MCPError("servidor caiu"))
        out = await ex()
        assert out == {"error": "servidor caiu"}

    @pytest.mark.asyncio
    async def test_generic_exception_becomes_typed_error(self, monkeypatch):
        monkeypatch.setattr("alpha._turn_budget.remaining", lambda: None)
        _, ex = self._executor_for(ValueError("args ruins"))
        out = await ex()
        assert out == {"error": "ValueError: args ruins"}

    @pytest.mark.asyncio
    async def test_timeout_clamped_to_turn_budget(self, monkeypatch):
        from alpha.mcp.client import TOOL_CALL_TIMEOUT

        monkeypatch.setattr("alpha._turn_budget.remaining", lambda: 5.0)
        client, ex = self._executor_for({"content": []})
        await ex()
        assert client.seen[0][2] == 5.0

        monkeypatch.setattr("alpha._turn_budget.remaining", lambda: None)
        client2, ex2 = self._executor_for({"content": []})
        await ex2()
        assert client2.seen[0][2] == TOOL_CALL_TIMEOUT
