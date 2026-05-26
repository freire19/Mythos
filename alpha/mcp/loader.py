"""Connect to MCP servers and register their tools in the alpha tool registry.

The loader is idempotent: calling `load_mcp_servers()` twice does nothing
the second time. `shutdown_mcp_servers()` terminates all subprocesses.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from ..tools import ToolDefinition, ToolSafety, register_tool
from .client import MCPClient, MCPError
from .config import MCPServerConfig, load_mcp_config

logger = logging.getLogger(__name__)

TOOL_PREFIX = "mcp__"
_active_clients: list[MCPClient] = []
_loaded = False

# AUDIT_V1.8 #034: cap total para o handshake completo (start+initialize+
# list_tools) por servidor MCP. Sem isso, um servidor com Popen lento
# (NFS filesystem, PID exhaustion, binario travado em busy-loop) pendurava
# o CLI por 60s+ (30s initialize + 30s list_tools) ou indefinidamente
# (Popen sem timeout). Cap defensivo: 5 servers x 15s = 75s pior caso.
_MCP_STARTUP_TIMEOUT = 15.0


def _qualified_name(server: str, tool: str) -> str:
    return f"{TOOL_PREFIX}{server}__{tool}"


def _format_tool_result(raw: dict) -> dict[str, Any]:
    """Convert an MCP tools/call result to the alpha tool-result shape.

    DEEP_SECURITY V3.3 #D119: campos `uri` de items do tipo "resource"
    eram concatenados crus em `f"[resource {uri}]\\n{text}"`. Um servidor
    MCP comprometido (ou um resource retornado por servidor legitimo que
    proxy-a conteudo de URL atacante) podia injetar `\\n\\n## SYSTEM: ...`
    ou ANSI escapes via uri, escapando do `[resource ...]` que o LLM usa
    como delimitador visual. Sanitizamos uri e text antes de concatenar.
    """
    if not isinstance(raw, dict):
        return {"output": _sanitize_mcp_text(str(raw))}

    parts: list[str] = []
    for item in raw.get("content", []) or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(_sanitize_mcp_text(str(item.get("text", ""))))
        elif kind == "resource":
            resource = item.get("resource", {})
            uri = _sanitize_mcp_uri(str(resource.get("uri", "")))
            text = _sanitize_mcp_text(str(resource.get("text", "")))
            parts.append(f"[resource {uri}]\n{text}" if text else f"[resource {uri}]")
        else:
            parts.append(f"[{kind} content omitted]")

    text = "\n".join(parts).strip()
    if raw.get("isError"):
        return {"error": text or "MCP tool returned isError without content"}
    return {"output": text} if text else {"output": ""}


# DEEP_SECURITY V3.3 #D119: stripping de bytes de controle e bidi-overrides.
# Mesma policy usada em `delegate_tools._strip_control_chars` para subagent
# prompts. NUL/ANSI/bidi sao os principais vetores de "esconder instrucoes
# no meio do output" em texto que o LLM le como contexto.
_MCP_CONTROL_CHARS = set(chr(c) for c in range(32) if c not in (9, 10, 13)) | {"\x7f"}
_MCP_BIDI_OVERRIDES = {
    "‪", "‫", "‬", "‭", "‮",  # LRE/RLE/PDF/LRO/RLO
    "⁦", "⁧", "⁨", "⁩",            # LRI/RLI/FSI/PDI
    "‎", "‏",                                # LRM/RLM
}
_MCP_FORBIDDEN_CHARS = _MCP_CONTROL_CHARS | _MCP_BIDI_OVERRIDES


def _sanitize_mcp_text(text: str) -> str:
    """Strip control chars + bidi overrides from MCP text content."""
    if not text:
        return text
    if not any(c in _MCP_FORBIDDEN_CHARS for c in text):
        return text  # hot-path: most content is clean
    return "".join(c for c in text if c not in _MCP_FORBIDDEN_CHARS)


def _sanitize_mcp_uri(uri: str) -> str:
    """Strip control chars + newlines from MCP resource URIs.

    URIs sao mais restritivos que texto livre — RFC 3986 limita a subset
    ASCII com pontuacao definida. Newlines, tabs e qualquer non-printable
    nao tem como aparecer em URI legitimo, entao bloqueamos integralmente.
    Limitamos tambem a 512 chars para evitar uri gigante poluir tool_result.
    """
    if not uri:
        return uri
    cleaned = "".join(c for c in uri if c.isprintable() and c not in ("\n", "\r", "\t"))
    if len(cleaned) > 512:
        cleaned = cleaned[:512] + "...[uri-truncated]"
    return cleaned


def _make_executor(client: MCPClient, tool_name: str):
    async def executor(**kwargs) -> dict[str, Any]:
        try:
            raw = await asyncio.to_thread(client.call_tool, tool_name, kwargs)
        except MCPError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        return _format_tool_result(raw)

    return executor


def _register_server_tools(client: MCPClient) -> int:
    count = 0
    for tool in client.tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        qualified = _qualified_name(client.name, name)
        params = tool.get("inputSchema") or {"type": "object", "properties": {}}
        register_tool(
            ToolDefinition(
                name=qualified,
                description=tool.get("description") or f"MCP tool {name} from {client.name}",
                parameters=params,
                safety=ToolSafety.DESTRUCTIVE,  # MCP tools require approval by default
                executor=_make_executor(client, name),
                category=f"mcp:{client.name}",
            )
        )
        count += 1
    return count


def _connect_one(
    spec: MCPServerConfig,
    startup_timeout: float = _MCP_STARTUP_TIMEOUT,
) -> MCPClient | None:
    """Conecta a um MCP server com cap absoluto no handshake completo.

    AUDIT_V1.8 #034: start+initialize+list_tools podem cada um ter seu
    proprio timeout interno (default 30s do client), mas Popen em si nao
    tem timeout — filesystem stall ou subprocess preso em busy-loop antes
    de aceitar JSON-RPC trava aqui ate o `wait_for_input` interno expirar.
    Wrapper threading.Thread.join(timeout) garante cap total e mata o
    subprocess se passar do limite. Daemon=True para nao bloquear sys.exit.
    """
    client = MCPClient(
        name=spec.name,
        command=spec.command,
        args=spec.args,
        env=spec.env,
    )
    result: dict[str, Any] = {}

    def _do_connect() -> None:
        try:
            client.start()
            client.initialize()
            client.list_tools()
            result["ok"] = True
        except MCPError as e:
            result["error"] = e
            result["category"] = "mcp_error"
        except Exception as e:
            result["error"] = e
            result["category"] = "unexpected"

    t = threading.Thread(
        target=_do_connect,
        name=f"mcp-connect-{spec.name}",
        daemon=True,
    )
    t.start()
    t.join(timeout=startup_timeout)

    if t.is_alive():
        # Handshake nao completou — mata o subprocess. Thread daemon vai
        # morrer com o processo se mesmo `client.stop()` nao conseguir
        # liberar (raro: stop usa terminate + wait 3s + kill).
        logger.warning(
            "MCP '%s' startup timeout (%.1fs) — killing subprocess",
            spec.name, startup_timeout,
        )
        try:
            client.stop()
        except Exception as stop_err:
            logger.debug(
                "MCP '%s' stop() apos timeout falhou: %s", spec.name, stop_err
            )
        return None

    if "ok" not in result:
        err = result.get("error", "unknown")
        category = result.get("category", "unknown")
        if category == "mcp_error":
            logger.warning("MCP '%s' failed to start: %s", spec.name, err)
        else:
            logger.warning("MCP '%s' unexpected error: %s", spec.name, err)
        try:
            client.stop()
        except Exception as stop_err:
            logger.debug(
                "MCP '%s' stop() apos error falhou: %s", spec.name, stop_err
            )
        return None
    return client


def load_mcp_servers() -> list[MCPClient]:
    """Spawn all enabled MCP servers and register their tools.

    Returns the list of clients that connected successfully. Failures are
    logged and skipped — a broken server config never blocks startup.
    """
    global _loaded
    if _loaded:
        return list(_active_clients)

    specs = load_mcp_config()
    enabled = [s for s in specs if not s.disabled]
    if not enabled:
        _loaded = True
        return []

    for spec in enabled:
        client = _connect_one(spec)
        if client is None:
            continue
        n = _register_server_tools(client)
        logger.info("MCP '%s' connected with %d tool(s)", client.name, n)
        _active_clients.append(client)

    _loaded = True
    return list(_active_clients)


def shutdown_mcp_servers() -> None:
    global _loaded
    for client in _active_clients:
        try:
            client.stop()
        except Exception as e:
            # #DM043: shutdown best-effort but log to diagnose zombie
            # subprocess leaks (was silent — could mask process-kill failures).
            logger.warning("MCP %s stop failed: %s", client.name, e)
    _active_clients.clear()
    _loaded = False


def list_active_servers() -> list[dict]:
    return [
        {"name": c.name, "tools": [t.get("name") for t in c.tools]}
        for c in _active_clients
    ]
