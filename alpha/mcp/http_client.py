"""Streamable-HTTP JSON-RPC client for a single remote MCP server.

Implementa o transporte "Streamable HTTP" da spec MCP (2025-06-18):
todas as mensagens JSON-RPC vão em POST para a URL do servidor; a
resposta chega como `application/json` (mensagem única) ou como
`text/event-stream` (SSE — o servidor pode intercalar notifications
antes da response com o `id` correspondente). Sessão via header
`Mcp-Session-Id` capturado no `initialize` e reenviado em toda request;
`DELETE` best-effort no encerramento.

Mesma superfície do `MCPClient` stdio (start/initialize/list_tools/
call_tool/stop, `.name`, `.tools`) — o loader trata os dois por
duck-typing. Cliente httpx SÍNCRONO de propósito: o CLI recria o event
loop a cada turno, e um client sync sobrevive entre loops (callers
async chegam via `asyncio.to_thread`, como no stdio).

Auth: headers estáticos do mcp.json (ex.: `Authorization: Bearer ${VAR}`,
expandido de env) OU OAuth 2.1 (`alpha.mcp.oauth`): sem header estático, o
client carrega o token salvo em `~/.alpha/mcp_tokens.json` no `start()`
(refresh silencioso se expirado). Um 401 sem token vira `MCPAuthRequired`
— o usuário roda `/mcp auth <server>` para o fluxo interativo (browser +
PKCE), que nunca acontece dentro do handshake de startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from urllib.parse import urlparse

import httpx

from ..net_utils import is_private_ip
from .client import (
    _NOTIFICATIONS_CAP,
    DEFAULT_REQUEST_TIMEOUT,
    PROTOCOL_VERSION,
    MCPError,
    _MCPProtocolMixin,
)

logger = logging.getLogger(__name__)

# Cap de resposta (JSON ou SSE acumulado) — um servidor malicioso/quebrado
# não pode empurrar payload ilimitado para a memória do CLI.
_MAX_RESPONSE_BYTES = 10_000_000
# AUDIT_V2.3 #302: o timeout do httpx é per-read (tempo entre chegadas de
# bytes) — um servidor que goteje 1 byte por vez nunca o estoura. Estes dois
# caps fecham o buraco: teto de LINHAS SSE processadas por request (um flood
# de eventos minúsculos/linhas em branco tem custo por-linha, não por-byte) e
# teto de TEMPO TOTAL do stream por time.monotonic().
_MAX_SSE_LINES = 100_000
_SSE_TOTAL_DEADLINE = 120.0  # segundos — alinhado ao TOOL_CALL_TIMEOUT

# Hosts onde http:// (sem TLS) é aceitável — loopback nunca sai da máquina.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def validate_mcp_url(url: str) -> str | None:
    """Retorna mensagem de erro se a URL for inaceitável, None se ok.

    https é obrigatório fora de loopback: headers de auth (Bearer) em
    http plano vazariam o token para a rede.
    """
    try:
        parsed = urlparse(url)
    except ValueError as e:
        return f"URL inválida: {e}"
    if parsed.scheme not in ("http", "https"):
        return f"Esquema não suportado: {parsed.scheme!r} (use https)"
    if not parsed.hostname:
        return "URL sem hostname"
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        return (
            "http:// só é permitido para loopback (localhost/127.0.0.1) — "
            "use https para servidores remotos (protege headers de auth)"
        )
    # #D304 + AUDIT_V2.3 #104: bloqueia SSRF para IP privado/reservado/
    # link-local/metadata (169.254.169.254). O guard central de net_utils
    # (getaddrinfo fail-closed) já existia mas não era usado aqui. Loopback
    # explícito é a exceção (também é "privado", mas http:// já foi restrito
    # a loopback acima). Escape hatch para MCP interno legítimo sobre https:
    # ALPHA_MCP_ALLOW_PRIVATE=1 (TOCTOU/DNS-rebinding residual aceito — o
    # mcp.json é config do usuário e stdio já dá primitivo pior).
    if (
        parsed.hostname not in _LOOPBACK_HOSTS
        and os.environ.get("ALPHA_MCP_ALLOW_PRIVATE", "").strip() != "1"
        and is_private_ip(parsed.hostname)
    ):
        return (
            f"SSRF bloqueado: {parsed.hostname} resolve para IP privado/"
            "reservado/link-local — MCP HTTP só aceita hosts públicos "
            "(ALPHA_MCP_ALLOW_PRIVATE=1 para liberar rede interna)"
        )
    return None


class MCPAuthRequired(MCPError):
    """401/403 from a server with no static auth header: the user must run
    `/mcp auth <server>` (interactive OAuth) — never raised mid-handshake
    as a generic error so the loader can give a precise hint."""

    def __init__(self, name: str, status: int, www_authenticate: str | None):
        self.server = name
        self.status = status
        self.www_authenticate = www_authenticate
        super().__init__(
            f"MCP '{name}' exige autenticação (HTTP {status}). "
            f"Rode `/mcp auth {name}` para autorizar via OAuth no navegador, "
            'ou configure "headers": {"Authorization": "Bearer ${TOKEN}"} em .alpha/mcp.json.'
        )


class MCPHttpClient(_MCPProtocolMixin):
    """Connection to one MCP server over Streamable HTTP."""

    def __init__(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        oauth: bool | None = None,
    ):
        self.name = name
        self.url = url
        self.extra_headers = dict(headers or {})
        self.tools: list[dict] = []
        self._session_id: str | None = None
        self._client: httpx.Client | None = None
        self._transport = transport  # injeção para testes (MockTransport)
        self._notifications: deque[dict] = deque(maxlen=_NOTIFICATIONS_CAP)
        self._lock = threading.Lock()
        self._next_id = 1
        # OAuth is the auth path whenever no static Authorization header is
        # configured (explicit `oauth=False` opts out).
        has_static_auth = any(k.lower() == "authorization" for k in self.extra_headers)
        self.oauth_enabled = (not has_static_auth) if oauth is None else bool(oauth)
        self._oauth_token: str | None = None

    # ── Lifecycle ──

    def start(self) -> None:
        err = validate_mcp_url(self.url)
        if err:
            raise MCPError(f"MCP '{self.name}': {err}")
        kwargs: dict = {
            "timeout": httpx.Timeout(DEFAULT_REQUEST_TIMEOUT, connect=10.0),
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.Client(**kwargs)
        if self.oauth_enabled:
            self.load_oauth_token()

    def load_oauth_token(self) -> bool:
        """Pick up the stored OAuth token for this URL (refreshing silently if
        expired). Non-interactive. Returns True when a token is in place."""
        try:
            from .oauth import cached_access_token
            self._oauth_token = cached_access_token(self.url, self._client)
        except Exception as e:  # token store trouble never blocks connect
            logger.debug("MCP %s: oauth token load failed: %s", self.name, e)
            self._oauth_token = None
        return self._oauth_token is not None

    def stop(self) -> None:
        if self._client is None:
            return
        # Spec: o client DEVERIA encerrar a sessão com DELETE. Best-effort —
        # servidores podem responder 405 (não implementado), e o close do
        # client segue de qualquer forma.
        if self._session_id:
            try:
                self._client.delete(
                    self.url,
                    headers=self._base_headers(),
                    # AUDIT_V2.3 #304: sem timeout curto explícito, o DELETE
                    # herdava os 30s do client — e `shutdown_mcp_servers`
                    # itera os clients sequencialmente, então um servidor
                    # travado congelava a saída do CLI por 30s POR servidor.
                    timeout=httpx.Timeout(3.0, connect=3.0),
                )
            except Exception as e:
                logger.debug("MCP %s: session DELETE failed: %s", self.name, e)
        try:
            self._client.close()
        finally:
            self._client = None
            self._session_id = None

    # ── Protocol: initialize/list_tools/call_tool herdados do
    # _MCPProtocolMixin (#500 — eram cópias byte-idênticas do stdio). ──

    # ── Internals ──

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.extra_headers,
        }
        if self._oauth_token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {self._oauth_token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, message: dict, timeout: float) -> httpx.Response:
        if self._client is None:
            raise MCPError(f"MCP server '{self.name}' is not running")
        try:
            req = self._client.build_request(
                "POST",
                self.url,
                json=message,
                headers=self._base_headers(),
                timeout=timeout,
            )
            return self._client.send(req, stream=True)
        except httpx.TimeoutException as e:
            raise MCPError(
                f"MCP '{self.name}' timed out on '{message.get('method')}' "
                f"after {timeout}s"
            ) from e
        except httpx.HTTPError as e:
            raise MCPError(f"MCP '{self.name}' transport error: {e}") from e

    def _check_status(self, resp: httpx.Response, method: str) -> None:
        if resp.status_code < 400:
            # Captura/renova o id de sessão quando o servidor enviar.
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            return
        # #D312: lê só ~200 bytes de preview (streaming), sem materializar o
        # corpo inteiro de uma resposta de erro potencialmente gigante.
        preview = bytearray()
        for chunk in resp.iter_bytes():
            preview += chunk
            if len(preview) >= 200:
                break
        body_preview = bytes(preview[:200]).decode("utf-8", errors="replace")
        if resp.status_code in (401, 403):
            if self.oauth_enabled:
                # A stale stored token is dropped so the next `/mcp auth`
                # (or a refresh) starts clean.
                self._oauth_token = None
                raise MCPAuthRequired(
                    self.name, resp.status_code, resp.headers.get("www-authenticate"),
                )
            raise MCPError(
                f"MCP '{self.name}' recusou autenticação (HTTP {resp.status_code}) "
                "em '" + method + "'. Confira o header de auth em .alpha/mcp.json "
                '(ex.: "headers": {"Authorization": "Bearer ${MEU_TOKEN}"}), ou remova-o '
                f"para usar OAuth interativo via `/mcp auth {self.name}`."
            )
        raise MCPError(
            f"MCP '{self.name}' HTTP {resp.status_code} on '{method}': {body_preview}"
        )

    def _read_capped(self, resp: httpx.Response, method: str) -> bytes:
        """#D312: lê o corpo incrementalmente abortando ao ultrapassar
        _MAX_RESPONSE_BYTES — o antigo `resp.read()` materializava o corpo
        inteiro antes do cap (DoS por corpo gigante)."""
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > _MAX_RESPONSE_BYTES:
                raise MCPError(
                    f"MCP '{self.name}' response exceeded "
                    f"{_MAX_RESPONSE_BYTES} bytes on '{method}'"
                )
        return bytes(buf)

    def _read_sse_response(self, resp: httpx.Response, req_id: int, method: str) -> dict:
        """Consome o stream SSE até chegar a response com o nosso id.

        Notifications (sem id) intercaladas vão para o buffer. Eventos são
        delimitados por linha em branco; linhas `data:` do mesmo evento são
        concatenadas (spec SSE).
        """
        data_lines: list[str] = []
        buf = bytearray()
        total = 0
        lines = 0
        # AUDIT_V2.3 #302: deadline de TEMPO TOTAL do stream. O timeout do
        # httpx é per-read; um servidor gotejando bytes (ou floodando linhas
        # em branco, que custam 1 byte cada) mantinha o worker to_thread —
        # não-cancelável — vivo indefinidamente.
        deadline = time.monotonic() + _SSE_TOTAL_DEADLINE
        # #D312: conta BYTES por chunk e faz o line-split manualmente — o antigo
        # `iter_lines()` bufferizava internamente um corpo sem newline, então o
        # cap por-linha nunca disparava (DoS).
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise MCPError(
                    f"MCP '{self.name}' SSE response exceeded "
                    f"{_MAX_RESPONSE_BYTES} bytes on '{method}'"
                )
            if time.monotonic() > deadline:
                raise MCPError(
                    f"MCP '{self.name}' SSE stream exceeded "
                    f"{_SSE_TOTAL_DEADLINE:.0f}s on '{method}'"
                )
            buf += chunk
            while b"\n" in buf:
                lines += 1
                if lines > _MAX_SSE_LINES:
                    raise MCPError(
                        f"MCP '{self.name}' SSE stream exceeded "
                        f"{_MAX_SSE_LINES} lines on '{method}'"
                    )
                raw, _, buf = buf.partition(b"\n")
                line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
                if line == "":
                    if not data_lines:
                        continue
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.debug("MCP %s: non-JSON SSE event discarded", self.name)
                        continue
                    handled = self._route_message(msg, req_id, method)
                    if handled is not None:
                        return handled
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # Outros campos SSE (event:, id:, retry:) são ignorados.
        raise MCPError(
            f"MCP '{self.name}' closed SSE stream without answering '{method}'"
        )

    def _route_message(self, msg: dict, req_id: int, method: str) -> dict | None:
        """Devolve o result se `msg` é a response do nosso request; senão
        bufferiza notifications e retorna None. Erros JSON-RPC viram MCPError."""
        if not isinstance(msg, dict):
            return None
        if msg.get("id") is None:
            self._notifications.append(msg)
            return None
        if str(msg.get("id")) != str(req_id):
            logger.debug(
                "MCP %s: response for unknown id %r discarded", self.name, msg.get("id")
            )
            return None
        if "error" in msg:
            err = msg["error"] or {}
            raise MCPError(
                f"MCP '{self.name}' returned error on '{method}': "
                f"{err.get('code')} {err.get('message')}"
            )
        return msg.get("result") or {}

    def _request(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict | None:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
        req: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            req["params"] = params

        resp = self._post(req, timeout)
        try:
            # #D811: as leituras do corpo (status/SSE/JSON) acontecem DEPOIS
            # do `send(stream=True)` — fora da tradução httpx→MCPError do
            # _post. Sem este guard, ReadTimeout/ReadError/RemoteProtocolError
            # de um servidor que estanca mid-body propagam crus, sem a
            # identidade do servidor nem do método.
            try:
                self._check_status(resp, method)
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    return self._read_sse_response(resp, req_id, method)
                body = self._read_capped(resp, method)
            except httpx.TimeoutException as e:
                raise MCPError(
                    f"MCP '{self.name}' timed out on '{method}' "
                    f"after {timeout}s"
                ) from e
            except httpx.HTTPError as e:
                raise MCPError(
                    f"MCP '{self.name}' transport error on '{method}': {e}"
                ) from e
            try:
                msg = json.loads(body)
            except json.JSONDecodeError as e:
                raise MCPError(
                    f"MCP '{self.name}' returned non-JSON response on '{method}'"
                ) from e
            handled = self._route_message(msg, req_id, method)
            if handled is None:
                raise MCPError(
                    f"MCP '{self.name}' response did not answer '{method}'"
                )
            return handled
        finally:
            resp.close()

    def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        resp = self._post(msg, DEFAULT_REQUEST_TIMEOUT)
        try:
            # Spec: notification aceita → 202 Accepted sem body. Erros de
            # status ainda importam (401 na notification indica auth quebrada).
            try:
                self._check_status(resp, method)
            except httpx.TimeoutException as e:
                raise MCPError(
                    f"MCP '{self.name}' timed out on '{method}' "
                    f"after {DEFAULT_REQUEST_TIMEOUT}s"
                ) from e
            except httpx.HTTPError as e:
                raise MCPError(
                    f"MCP '{self.name}' transport error on '{method}': {e}"
                ) from e
        finally:
            resp.close()
