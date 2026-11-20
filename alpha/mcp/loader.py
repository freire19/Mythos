"""Connect to MCP servers and register their tools in the alpha tool registry.

The loader is idempotent: calling `load_mcp_servers()` twice does nothing
the second time. `shutdown_mcp_servers()` terminates all subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait
from typing import Any

import httpx

from .._text_safety import strip_control_and_bidi
from ..tools import ToolDefinition, ToolSafety, register_tool
from .client import MCPClient, MCPError
from .config import MCPServerConfig, load_mcp_config
from .http_client import MCPAuthRequired, MCPHttpClient

logger = logging.getLogger(__name__)

# Ambos os transportes expõem a mesma superfície (duck-typed): start/stop/
# initialize/list_tools/call_tool + .name/.tools.
AnyMCPClient = MCPClient | MCPHttpClient

TOOL_PREFIX = "mcp__"
_active_clients: list[AnyMCPClient] = []
_loaded = False

# AUDIT_V1.8 #034: cap total para o handshake completo (start+initialize+
# list_tools) por servidor MCP. Sem isso, um servidor com Popen lento
# (NFS filesystem, PID exhaustion, binario travado em busy-loop) pendurava
# o CLI por 60s+ (30s initialize + 30s list_tools) ou indefinidamente
# (Popen sem timeout). Cap defensivo: 5 servers x 15s = 75s pior caso.
_MCP_STARTUP_TIMEOUT = 15.0
# #D812: teto AGREGADO do startup — os connects rodam em paralelo, mas um
# lote grande de servidores travados não pode segurar o prompt além disso.
_MCP_GLOBAL_STARTUP_BUDGET = 30.0
# #D814: retry curto SÓ no handshake (initialize/list_tools são idempotentes;
# tools/call NÃO é — segue single-shot) em erro de transporte transitório.
_HANDSHAKE_RETRIES = 1
_HANDSHAKE_BACKOFF = 0.5


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
            # #D326: `kind` é controlado pelo servidor MCP — sanitiza como label
            # estrutural (remove non-printables/newlines, cap curto) para não
            # injetar `]\n## SYSTEM:` no tool_result que o LLM lê.
            kind_label = _sanitize_mcp_uri(str(kind))[:32] or "unknown"
            parts.append(f"[{kind_label} content omitted]")

    text = "\n".join(parts).strip()
    if raw.get("isError"):
        return {"error": text or "MCP tool returned isError without content"}
    return {"output": text} if text else {"output": ""}


# DEEP_SECURITY V3.3 #D119 + AUDIT_V2.3 #501: stripping de bytes de controle
# e bidi-overrides. Policy única em `alpha._text_safety` — a mesma usada em
# `_delegate_core._strip_control_chars` para subagent prompts (o set literal
# vivia copiado verbatim nos dois módulos). NUL/ANSI/bidi sao os principais
# vetores de "esconder instrucoes no meio do output" em texto que o LLM le
# como contexto. Já em um único passe C via translation table (#DP024).
_sanitize_mcp_text = strip_control_and_bidi


def _sanitize_mcp_uri(uri: str) -> str:
    """Strip control chars + newlines from MCP resource URIs.

    URIs sao mais restritivos que texto livre — RFC 3986 limita a subset
    ASCII com pontuacao definida. Newlines, tabs e qualquer non-printable
    nao tem como aparecer em URI legitimo, entao bloqueamos integralmente.
    Limitamos tambem a 512 chars para evitar uri gigante poluir tool_result.
    """
    if not uri:
        return uri
    head = uri[:513]
    cleaned = "".join(c for c in head if c.isprintable() and c not in ("\n", "\r", "\t"))
    if len(cleaned) > 512 or len(uri) > 512:
        cleaned = cleaned[:512] + "...[uri-truncated]"
    return cleaned


# #D311: o nome QUALIFICADO (`mcp__<server>__<tool>`) vai para a API OpenAI
# (que exige `^[a-zA-Z0-9_-]{1,64}$`) e para o schema do LLM. Um nome com
# control/bidi/newline entrava verbatim (prompt-injection + quebra de API).
_MCP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MCP_DESC_CAP = 8000
# #D313: caps contra DoS de contexto/custo por servidor MCP hostil — tools/list
# só era limitado pelos ~10MB do transporte, então milhares de tools ou schemas
# gigantes eram serializados ao LLM a cada turno.
_MCP_MAX_TOOLS_PER_SERVER = 128
_MCP_MAX_SCHEMA_BYTES = 32_000
# AUDIT_V2.3 #102: profundidade máxima do inputSchema sanitizado. O transporte
# aceita até ~10MB — um schema pequeno mas patologicamente aninhado (milhares
# de arrays/objects) estouraria a recursão ANTES do cap de bytes do #D313.
_MCP_MAX_SCHEMA_DEPTH = 12


def _sanitize_schema(obj, depth: int = 0):
    """#D311/#102: strip recursivo de control/bidi das STRINGS de um JSON
    schema vindo do servidor MCP (description/title/enum que o LLM vê),
    preservando a estrutura. Aplica a mesma policy dos resultados
    (`_sanitize_mcp_text`). Containers além de `_MCP_MAX_SCHEMA_DEPTH`
    níveis são descartados (esvaziados) em vez de recursar."""
    if isinstance(obj, str):
        return _sanitize_mcp_text(obj)
    if isinstance(obj, list):
        return [] if depth >= _MCP_MAX_SCHEMA_DEPTH else [
            _sanitize_schema(v, depth + 1) for v in obj
        ]
    if isinstance(obj, dict):
        return {} if depth >= _MCP_MAX_SCHEMA_DEPTH else {
            k: _sanitize_schema(v, depth + 1) for k, v in obj.items()
        }
    return obj


def _make_executor(client: AnyMCPClient, tool_name: str):
    async def executor(**kwargs) -> dict[str, Any]:
        # AUDIT_V2.2 #011: cap the MCP call at the remaining turn budget so a
        # slow server doesn't keep a non-cancellable to_thread worker blocked
        # (and the MCP connection tied up) long after the agent gave up.
        from .._turn_budget import remaining as _remaining_budget
        from .client import TOOL_CALL_TIMEOUT
        budget = _remaining_budget()
        # #D813: budget desconhecido (inf/None) ≠ budget exaurido (<=0) —
        # o segundo caso ganha um floor fail-fast em vez dos 120s cheios.
        if budget is None or math.isinf(budget):
            call_timeout = TOOL_CALL_TIMEOUT
        elif budget <= 0:
            call_timeout = 2.0
        else:
            call_timeout = min(TOOL_CALL_TIMEOUT, budget)
        try:
            raw = await asyncio.to_thread(
                client.call_tool, tool_name, kwargs, call_timeout
            )
        except MCPError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        return _format_tool_result(raw)

    return executor


def _register_server_tools(client: AnyMCPClient) -> int:
    count = 0
    for tool in client.tools:
        if count >= _MCP_MAX_TOOLS_PER_SERVER:
            logger.warning(
                "MCP servidor '%s': mais de %d tools — restantes ignoradas (#D313).",
                client.name, _MCP_MAX_TOOLS_PER_SERVER,
            )
            break
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        qualified = _qualified_name(client.name, name)
        # #D311: rejeita nome qualificado inválido (control/bidi/newline ou fora
        # de ^[a-zA-Z0-9_-]{1,64}$) em vez de registrar verbatim; sanitiza a
        # description (com cap) e o inputSchema recursivamente.
        if not _MCP_NAME_RE.match(qualified):
            logger.warning(
                "MCP tool %r do servidor '%s': nome qualificado %r inválido "
                "(fora de ^[a-zA-Z0-9_-]{1,64}$) — tool ignorada (#D311).",
                name, client.name, qualified,
            )
            continue
        raw_desc = tool.get("description") or f"MCP tool {name} from {client.name}"
        description = _sanitize_mcp_text(str(raw_desc))[:_MCP_DESC_CAP]
        params = _sanitize_schema(
            tool.get("inputSchema") or {"type": "object", "properties": {}}
        )
        # #D313: schema gigante infla o prompt a cada turno — troca por
        # placeholder VÁLIDO (truncar a string quebraria o tool-calling).
        if len(json.dumps(params, ensure_ascii=False)) > _MCP_MAX_SCHEMA_BYTES:
            logger.warning(
                "MCP tool %r do servidor '%s': inputSchema excede %d bytes — "
                "substituído por placeholder (#D313).",
                name, client.name, _MCP_MAX_SCHEMA_BYTES,
            )
            params = {"type": "object", "properties": {}}
            description = (description + " [schema omitido: excedeu o cap]")[:_MCP_DESC_CAP]
        register_tool(
            ToolDefinition(
                name=qualified,
                description=description,
                parameters=params,
                safety=ToolSafety.DESTRUCTIVE,  # MCP tools require approval by default
                executor=_make_executor(client, name),
                category=f"mcp:{client.name}",
            )
        )
        count += 1
    return count


def _transient_transport_error(e: MCPError) -> bool:
    """#D814: erro de transporte transitório (vale retry no handshake)?

    HTTP: o `_post`/reads traduzem httpx→MCPError com `from e`, então a
    causa preserva o tipo. Stdio: EOF/pipe quebrado viram MCPError com
    mensagens fixas do client. Erros de protocolo (JSON-RPC error, HTTP
    4xx/5xx) NÃO são transitórios.
    """
    if isinstance(e.__cause__, httpx.TransportError):
        return True
    msg = str(e)
    return "closed connection" in msg or "write failed" in msg


def _connect_one(
    spec: MCPServerConfig,
    startup_timeout: float = _MCP_STARTUP_TIMEOUT,
) -> AnyMCPClient | None:
    """Conecta a um MCP server com cap absoluto no handshake completo.

    AUDIT_V1.8 #034: start+initialize+list_tools podem cada um ter seu
    proprio timeout interno (default 30s do client), mas Popen em si nao
    tem timeout — filesystem stall ou subprocess preso em busy-loop antes
    de aceitar JSON-RPC trava aqui ate o `wait_for_input` interno expirar.
    Wrapper threading.Thread.join(timeout) garante cap total e mata o
    subprocess se passar do limite. Daemon=True para nao bloquear sys.exit.
    """
    client: AnyMCPClient
    if spec.is_http:
        client = MCPHttpClient(
            name=spec.name,
            url=spec.url,
            headers=spec.headers,
            oauth=False if spec.auth == "none" else None,
        )
    else:
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
            for attempt in range(_HANDSHAKE_RETRIES + 1):
                try:
                    client.initialize()
                    client.list_tools()
                    break
                except MCPError as e:
                    if attempt >= _HANDSHAKE_RETRIES or not _transient_transport_error(e):
                        raise
                    logger.debug(
                        "MCP '%s': erro transitório no handshake (%s) — "
                        "retry %d/%d", spec.name, e, attempt + 1, _HANDSHAKE_RETRIES,
                    )
                    time.sleep(_HANDSHAKE_BACKOFF)
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
        if isinstance(err, MCPAuthRequired):
            # Not a failure of the config — the user simply hasn't authorized
            # yet. The message already says `/mcp auth <name>`.
            logger.warning("MCP '%s': %s", spec.name, err)
        elif category == "mcp_error":
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


def load_mcp_servers() -> list[AnyMCPClient]:
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

    # #D812: connect em paralelo com teto agregado — o loop sequencial
    # antigo somava até N×15s de stall no prompt quando vários servidores
    # travavam. Cada _connect_one preserva sua semântica (cap de 15s,
    # kill do subprocess, logs); o registro das tools fica na main thread.
    pool = ThreadPoolExecutor(
        max_workers=min(len(enabled), 8), thread_name_prefix="mcp-connect"
    )
    futures = [(spec, pool.submit(_connect_one, spec)) for spec in enabled]
    _futures_wait(
        [f for _, f in futures], timeout=_MCP_GLOBAL_STARTUP_BUDGET
    )
    pool.shutdown(wait=False, cancel_futures=True)
    for spec, fut in futures:
        if not fut.done() or fut.cancelled():
            logger.warning(
                "MCP '%s' descartado: estourou o budget global de startup "
                "(%.0fs)", spec.name, _MCP_GLOBAL_STARTUP_BUDGET,
            )
            fut.add_done_callback(_stop_late_client)
            continue
        try:
            client = fut.result()
        except Exception as e:
            logger.warning("MCP '%s' unexpected error: %s", spec.name, e)
            continue
        if client is None:
            continue
        n = _register_server_tools(client)
        logger.info("MCP '%s' connected with %d tool(s)", client.name, n)
        _active_clients.append(client)

    _loaded = True
    return list(_active_clients)


def _stop_late_client(fut) -> None:
    """Encerra um client cujo connect terminou DEPOIS do budget global —
    já foi descartado, então não pode ficar vivo sem registro."""
    if fut.cancelled():
        return
    try:
        client = fut.result()
    except Exception:
        return
    if client is None:
        return
    try:
        client.stop()
    except Exception as e:
        logger.debug("MCP late client stop failed: %s", e)


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
        {
            "name": c.name,
            "tools": [t.get("name") for t in c.tools],
            "resources": bool(getattr(c, "supports_resources", lambda: False)()),
        }
        for c in _active_clients
    ]


def get_client(name: str) -> AnyMCPClient | None:
    """Active client by server name (for the mcp_* resource tools)."""
    for c in _active_clients:
        if c.name == name:
            return c
    return None


def find_spec(name: str) -> MCPServerConfig | None:
    for spec in load_mcp_config():
        if spec.name == name:
            return spec
    return None


def connect_server(name: str) -> AnyMCPClient:
    """(Re)connect one configured server by name and register its tools —
    used by `/mcp auth` after an OAuth login. Raises MCPError on failure."""
    spec = find_spec(name)
    if spec is None:
        raise MCPError(f"MCP server '{name}' não está em .alpha/mcp.json")
    old = get_client(name)
    if old is not None:
        try:
            old.stop()
        except Exception as e:
            logger.debug("MCP %s stop before reconnect failed: %s", name, e)
        _active_clients.remove(old)
        from ..tools import TOOL_REGISTRY
        for tname in [t for t in TOOL_REGISTRY if t.startswith(_qualified_name(name, ""))]:
            TOOL_REGISTRY.pop(tname, None)
    client = _connect_one(spec)
    if client is None:
        raise MCPError(f"MCP '{name}' não conectou — veja o log acima.")
    n = _register_server_tools(client)
    _active_clients.append(client)
    logger.info("MCP '%s' (re)connected with %d tool(s)", name, n)
    return client
