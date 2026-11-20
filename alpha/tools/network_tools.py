"""HTTP request tools for ALPHA agent.

Provides safe HTTP client capabilities with SSRF protection.

SECURITY: Private/internal IPs are blocked by default to prevent SSRF.
Request size and timeout are limited. No credential forwarding.
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl as _ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse

from ..net_utils import (
    validate_url_syntax as _validate_url_syntax,
    resolve_and_validate as _resolve_and_validate,
)
from ..config import RETRY, TOOL_TIMEOUTS
from ._subprocess_helpers import STDERR_CAP, STDOUT_CAP
from .._loop_lock import LoopAwareLock
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)

_MAX_RESPONSE_SIZE = 1_000_000  # 1MB
_DEFAULT_SSL_CTX: _ssl.SSLContext | None = None


def _get_default_ssl_context() -> _ssl.SSLContext:
    global _DEFAULT_SSL_CTX
    if _DEFAULT_SSL_CTX is None:
        _DEFAULT_SSL_CTX = _ssl.create_default_context()
    return _DEFAULT_SSL_CTX



_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# Retry config (#DM036): centralizado em config.RETRY["http"].
# Metodos seguros (GET/HEAD/OPTIONS) podem retentar erros transientes
# sem risco de duplicar efeito. Escrita NUNCA retenta.


# #D008-PERF: aiohttp.ClientSession compartilhada por loop. Antes, cada
# `http_request` criava uma session nova (com connector pool, DNS cache,
# SSL context vazio) e fechava no fim. Cost amortizado se varias chamadas
# rodam sequencialmente — keep-alive das conexoes TCP resta valido por
# 75s default no aiohttp. Loop-aware pelo mesmo motivo de llm.py: single-shot
# CLI cria loop novo a cada invocacao mas o modulo persiste em cache.
_shared_aiohttp_session = None  # type: ignore[var-annotated]
_aiohttp_session_loop: object | None = None
# #D044: loop-aware lock — a plain module-level asyncio.Lock() binds to the
# first loop that contends it and crashes on the next REPL turn's loop.
_aiohttp_session_lock = LoopAwareLock()


async def _get_shared_aiohttp_session():
    """Lazy + loop-aware singleton de `aiohttp.ClientSession`."""
    global _shared_aiohttp_session, _aiohttp_session_loop
    import aiohttp

    loop = asyncio.get_running_loop()
    # Fast path: no lock needed when session is healthy and loop matches.
    if (
        _shared_aiohttp_session is not None
        and not _shared_aiohttp_session.closed
        and _aiohttp_session_loop is loop
    ):
        return _shared_aiohttp_session
    # AUDIT_V1.2 #006: lock protects the close() + reassign window.
    async with _aiohttp_session_lock():
        if (
            _shared_aiohttp_session is not None
            and not _shared_aiohttp_session.closed
            and _aiohttp_session_loop is loop
        ):
            return _shared_aiohttp_session
        if (
            _shared_aiohttp_session is not None
            and not _shared_aiohttp_session.closed
        ):
            try:
                await _shared_aiohttp_session.close()
            except Exception as e:
                # AUDIT_V2.2 #018: include the error type so a recurring close
                # failure (e.g. stuck connections) is diagnosable.
                logger.debug(
                    "aiohttp session close failed: %s: %s", type(e).__name__, e
                )
        _shared_aiohttp_session = aiohttp.ClientSession()
        _aiohttp_session_loop = loop
    return _shared_aiohttp_session


def _rewrite_url_with_ip(parsed, resolved_ip: str) -> str:
    """Reconstroi URL trocando hostname pelo IP resolvido.

    urlunparse evita falhas em hostnames uppercase ou IPv6 que `str.replace`
    deixaria passar (DNS rebinding window). IPv6 ganha brackets no netloc.
    """
    ip_for_url = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip
    netloc = f"{ip_for_url}:{parsed.port}" if parsed.port else ip_for_url
    parts = list(parsed)
    parts[1] = netloc
    return urlunparse(parts)


async def _http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Make an HTTP request."""
    if timeout is None:
        timeout = TOOL_TIMEOUTS.get("network", 30)

    method = method.upper()
    if method not in _ALLOWED_METHODS:
        return {
            "error": f"Método '{method}' não permitido. Use: {', '.join(sorted(_ALLOWED_METHODS))}"
        }

    # Pre-check sintatico (scheme/hostname); a validacao de IP fica na
    # unica resolucao async (_resolve_and_validate), fora da thread do loop.
    url_error = _validate_url_syntax(url)
    if url_error:
        return {"error": url_error, "blocked": True}

    from ..config import TOOL_TIMEOUT_CAPS
    timeout = min(timeout, TOOL_TIMEOUT_CAPS.get("network", 60))

    try:
        import aiohttp  # noqa: F401 — usado por aiohttp.ClientTimeout abaixo

        req_headers = dict(headers) if headers else {}
        req_headers.setdefault("User-Agent", "ALPHA-Agent/1.0")

        parsed = urlparse(url)
        hostname = parsed.hostname

        # Resolver DNS e validar IP ANTES de conectar (previne DNS rebinding)
        try:
            resolved_ip = await _resolve_and_validate(hostname)
        except ValueError as e:
            return {"error": str(e), "blocked": True}

        # Construir URL com IP fixo + header Host para SNI/virtual hosts
        fixed_url = _rewrite_url_with_ip(parsed, resolved_ip)
        req_headers["Host"] = hostname

        # HTTPS: cert e SNI precisam validar contra o hostname original,
        # nao o IP literal usado na conexao. Ssl context + server_hostname
        # garantem isso.
        is_https = parsed.scheme == "https"
        ssl_param = _get_default_ssl_context() if is_https else False

        session = await _get_shared_aiohttp_session()
        req_data = None
        if body and method in ("POST", "PUT", "PATCH"):
            content_type = req_headers.get("Content-Type", "")
            if "json" in content_type or (body.startswith("{") or body.startswith("[")):
                req_headers.setdefault("Content-Type", "application/json")
            req_data = body

        req_kwargs = dict(
            method=method,
            url=fixed_url,
            headers=req_headers,
            data=req_data,
            allow_redirects=False,  # NÃO seguir automaticamente
            timeout=aiohttp.ClientTimeout(total=timeout),
            ssl=ssl_param,
        )
        if is_https:
            req_kwargs["server_hostname"] = hostname
        resp = await session.request(**req_kwargs)

        try:
            # Manual redirect following com re-validação DNS+IP
            redirect_count = 0
            while resp.status in (301, 302, 303, 307, 308) and redirect_count < 5:
                redirect_url = resp.headers.get("Location")
                if not redirect_url:
                    break

                # Resolver URL relativo
                if redirect_url.startswith("/"):
                    port = parsed.port
                    port_str = f":{port}" if port else ""
                    redirect_url = f"{parsed.scheme}://{hostname}{port_str}{redirect_url}"

                # Re-validar DNS + IP do destino do redirect
                redirect_parsed = urlparse(redirect_url)
                redirect_hostname = redirect_parsed.hostname
                if not redirect_hostname:
                    break

                try:
                    redirect_ip = await _resolve_and_validate(redirect_hostname)
                except ValueError as e:
                    return {"error": f"Redirect bloqueado: {e}", "blocked": True}

                fixed_redirect = _rewrite_url_with_ip(redirect_parsed, redirect_ip)
                req_headers["Host"] = redirect_hostname

                # Headers hop-sensitive nao atravessam para outro host.
                if redirect_hostname.lower() != (hostname or "").lower():
                    for key in list(req_headers):
                        if key.lower() in (
                            "authorization",
                            "cookie",
                            "proxy-authorization",
                        ):
                            del req_headers[key]

                # 301/302/303: POST vira GET sem body (convencao);
                # 307/308: metodo E body preservados (RFC 7538).
                if resp.status in (301, 302, 303) and method == "POST":
                    method = "GET"
                    req_data = None

                redirect_is_https = redirect_parsed.scheme == "https"
                redirect_ssl = (
                    _get_default_ssl_context() if redirect_is_https else False
                )
                redirect_kwargs = dict(
                    method=method,
                    url=fixed_redirect,
                    headers=req_headers,
                    data=req_data,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    ssl=redirect_ssl,
                )
                if redirect_is_https:
                    redirect_kwargs["server_hostname"] = redirect_hostname
                # Release the previous response before starting the next one
                # so its connection returns to the pool immediately.
                old_resp = resp
                resp = await session.request(**redirect_kwargs)
                old_resp.release()
                redirect_count += 1

            # #D314: lê em streaming abortando ao passar do cap — o antigo
            # `resp.read()` bufferizava o corpo INTEIRO antes de truncar (OOM
            # sob prompt-injection para um servidor de corpo gigante).
            raw = bytearray()
            truncated = False
            async for chunk in resp.content.iter_chunked(65536):
                raw += chunk
                if len(raw) > _MAX_RESPONSE_SIZE:
                    truncated = True
                    break
        finally:
            # Release also covers the read raising mid-stream.
            resp.release()
        if truncated:
            body_text = bytes(raw[:_MAX_RESPONSE_SIZE]).decode(errors="replace")
            body_text += f"\n... [truncado: resposta > {_MAX_RESPONSE_SIZE // 1000}KB]"
        else:
            body_text = bytes(raw).decode(errors="replace")

        resp_headers = dict(resp.headers)

        return {
            "status_code": resp.status,
            "headers": {
                k: v
                for k, v in resp_headers.items()
                if k.lower()
                in (
                    "content-type",
                    "content-length",
                    "server",
                    "date",
                    "location",
                    "x-request-id",
                    "etag",
                )
            },
            "body": body_text[:STDOUT_CAP],
            "url": str(resp.url),
            "method": method,
        }

    except ImportError:
        # Fallback: use urllib (stdlib)
        return await _http_request_urllib(url, method, headers, body, timeout)
    except TimeoutError:
        return {"error": f"Request excedeu timeout de {timeout}s", "timeout": True}
    except (OSError, ConnectionError) as e:
        # #057: erros de I/O transientes (DNS hiccup, conexao resetada)
        # em metodos seguros podem ser retentados. O caller pega `error` +
        # `transient: True` se quiser retry manual, ou usa _http_request_retry.
        return {
            "error": f"Erro de I/O na requisição: {type(e).__name__}: {e}",
            "transient": True,
        }
    except Exception as e:
        # AUDIT_V2.2 #014: aiohttp ClientPayloadError / ServerDisconnectedError
        # subclass ClientError (not OSError), so without this they'd fall here
        # unmarked and _http_request_with_retry would never retry them on an
        # idempotent GET. Mark the known-transient ones.
        out = {"error": f"Erro na requisição: {type(e).__name__}: {e}"}
        if isinstance(
            e,
            (
                aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientPayloadError,
            ),
        ):
            out["transient"] = True
        return out


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from following redirects (DNS rebinding protection)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def _http_request_urllib(
    url: str, method: str, headers: dict, body: str, timeout: int
) -> dict:
    """Fallback HTTP using urllib (stdlib)."""
    # Resolve DNS and validate IP BEFORE connecting (prevents DNS rebinding)
    parsed = urlparse(url)
    hostname = parsed.hostname
    resolved_ip = None
    if hostname:
        try:
            resolved_ip = await _resolve_and_validate(hostname)
        except ValueError as e:
            return {"error": str(e), "blocked": True}

    try:
        # Build opener that does NOT follow redirects (DNS rebinding fix)
        opener = urllib.request.build_opener(_NoRedirectHandler)
        # Use resolved IP in URL to prevent DNS rebinding between validate and connect.
        # AUDIT_V1.2 #016: reusa `_rewrite_url_with_ip` (urlunparse-based) em vez de
        # `str.replace` que falhava com hostnames uppercase e IPv6.
        if resolved_ip and hostname:
            fixed_url = _rewrite_url_with_ip(parsed, resolved_ip)
        else:
            fixed_url = url
        req = urllib.request.Request(fixed_url, method=method)
        req.add_header("User-Agent", "ALPHA-Agent/1.0")
        req.add_header("Host", hostname or "")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        data = body.encode() if body else None

        resp = await asyncio.wait_for(
            asyncio.to_thread(lambda: opener.open(req, data=data, timeout=min(timeout, 30))),
            timeout=timeout + 5,
        )

        try:
            resp_body = resp.read(_MAX_RESPONSE_SIZE).decode(errors="replace")
        finally:
            resp.close()
        return {
            "status_code": resp.status,
            "headers": dict(resp.headers),
            "body": resp_body[:STDOUT_CAP],
            "url": url,
            "method": method,
        }
    except urllib.error.HTTPError as e:
        # #039: reading the error body can itself raise (connection cut
        # mid-read) — guard it so the handler returns a structured result
        # instead of propagating a raw exception out of the except block.
        try:
            err_body = e.read(_MAX_RESPONSE_SIZE).decode(errors="replace")[:STDERR_CAP]
        except Exception as read_err:
            err_body = f"<corpo de erro ilegível: {type(read_err).__name__}>"
        return {
            "status_code": e.code,
            "error": str(e.reason),
            "body": err_body,
        }
    except (TimeoutError, ConnectionError, OSError) as e:
        # AUDIT_V2.2 #015: keep the urllib fallback path consistent with the
        # aiohttp path — surface the type and mark transient so idempotent GETs
        # can be retried.
        return {"error": f"{type(e).__name__}: {e}", "transient": True}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _http_request_with_retry(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Wrapper do `_http_request` com retry de transientes em metodos seguros.

    Metodos de escrita (POST/PUT/PATCH/DELETE) NUNCA retentam (#057): se
    o request criou recurso parcialmente antes do erro, retentar duplica.
    GET/HEAD/OPTIONS sao idempotentes — retentam erros marcados como
    `transient` por `_http_request`.
    """
    method_upper = (method or "GET").upper()
    if method_upper not in RETRY["http"]["safe_methods"]:
        return await _http_request(url, method, headers, body, timeout)

    last_result: dict = {}
    for attempt in range(RETRY["http"]["max_retries"] + 1):
        result = await _http_request(url, method, headers, body, timeout)
        if not result.get("transient"):
            return result
        last_result = result
        if attempt < RETRY["http"]["max_retries"]:
            # #032: cap the exponential growth at max_backoff before applying
            # jitter, so a future bump to max_retries can't produce an
            # unbounded sleep. Jitter (≤+30%) still desyncs concurrent retries.
            base = min(
                RETRY["http"]["initial_backoff"] * (2 ** attempt),
                RETRY["http"]["max_backoff"],
            )
            backoff = base * (1 + random.random() * 0.3)
            logger.warning(
                f"http_request transient error on {url} "
                f"(attempt {attempt + 1}/{RETRY['http']['max_retries'] + 1}), "
                f"retrying in {backoff:.2f}s"
            )
            await asyncio.sleep(backoff)
    # Esgotou retries — retorna o ultimo erro com a info do retry budget.
    last_result["retried"] = RETRY["http"]["max_retries"] + 1
    return last_result


register_tool(
    ToolDefinition(
        name="http_request",
        description=(
            "Make an HTTP request (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS). "
            "Supports custom headers and body. SSRF protection: private IPs are blocked. "
            "Useful for testing APIs, checking endpoints, downloading data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL (http:// or https://)",
                },
                "method": {
                    "type": "string",
                    "description": "HTTP method",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": "HTTP headers as an object {key: value}",
                },
                "body": {
                    "type": "string",
                    "description": "Request body (for POST, PUT, PATCH). JSON as a string.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (max 60). Default: 30",
                    "default": 30,
                },
            },
            "required": ["url"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.NETWORK,
        executor=_http_request_with_retry,
    )
)
