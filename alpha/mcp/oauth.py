"""Interactive OAuth 2.1 for remote MCP servers (parity wave 2026-11, item 10).

Implements the MCP authorization spec on the client side:

1. **Discovery** — the server's `WWW-Authenticate: Bearer resource_metadata=…`
   (or `<origin>/.well-known/oauth-protected-resource`) names the
   authorization server; its `/.well-known/oauth-authorization-server`
   (fallback `/.well-known/openid-configuration`) gives the endpoints.
2. **Dynamic client registration** (RFC 7591) when the server offers a
   `registration_endpoint` and no `client_id` was configured.
3. **Authorization code + PKCE (S256)** — opens the browser, receives the
   code on a loopback `http://127.0.0.1:<port>/callback` listener, checks
   `state`, exchanges the code at the token endpoint (with `resource` per
   RFC 8707).
4. **Token store** — `~/.alpha/mcp_tokens.json` (0600, atomic), keyed by
   server URL; access tokens are refreshed with the refresh token when
   expired, silently, at connect time.

The interactive step never runs inside the startup handshake (15s cap,
daemon thread): a 401 at startup becomes `MCPAuthRequired` and the user
runs `/mcp auth <server>` in the REPL, which drives this flow in the
foreground and reconnects. Headless runs (`-p`) never pop a browser.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import re
import secrets
import threading
import time
import webbrowser
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit

import httpx

from ..settings import alpha_user_dir, atomic_write_text, read_json

logger = logging.getLogger(__name__)

TOKENS_FILE = "mcp_tokens.json"
CALLBACK_PATH = "/callback"
AUTH_TIMEOUT_S = 300.0
_HTTP_TIMEOUT = 15.0
_CLIENT_NAME = "alpha-code"
_EXPIRY_SKEW_S = 60.0


class OAuthError(RuntimeError):
    """User-facing OAuth failure (discovery, registration, exchange…)."""


# ── token store ──────────────────────────────────────────────────────


def _store_path():
    return alpha_user_dir() / TOKENS_FILE


def _load_store() -> dict:
    p = _store_path()
    if not p.exists():  # a fresh install has no store — not worth a warning
        return {}
    data = read_json(p, default={})
    return data if isinstance(data, dict) else {}


def _save_store(data: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600)


def get_stored(url: str) -> dict | None:
    entry = _load_store().get(url)
    return entry if isinstance(entry, dict) else None


def put_stored(url: str, entry: dict) -> None:
    data = _load_store()
    data[url] = entry
    _save_store(data)


def forget(url: str) -> bool:
    data = _load_store()
    if url not in data:
        return False
    data.pop(url, None)
    _save_store(data)
    return True


def _is_expired(entry: dict) -> bool:
    exp = entry.get("expires_at")
    if not isinstance(exp, (int, float)):
        return False  # no expiry reported → assume valid until a 401 says otherwise
    return time.time() >= exp - _EXPIRY_SKEW_S


# ── discovery ────────────────────────────────────────────────────────

_RESOURCE_META_RE = re.compile(r'resource_metadata="([^"]+)"')
_SCOPE_RE = re.compile(r'scope="([^"]+)"')


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _get_json(http: httpx.Client, url: str) -> dict | None:
    try:
        r = http.get(url, timeout=_HTTP_TIMEOUT, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        logger.debug("oauth discovery GET %s failed: %s", url, e)
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def discover(server_url: str, http: httpx.Client, www_authenticate: str | None = None) -> dict:
    """Resolve authorization-server metadata for `server_url`.

    Returns {authorization_endpoint, token_endpoint, registration_endpoint?,
    resource, scopes (list), issuer}."""
    resource = server_url
    scopes: list[str] = []
    meta_urls: list[str] = []
    if www_authenticate:
        m = _RESOURCE_META_RE.search(www_authenticate)
        if m:
            meta_urls.append(m.group(1))
        s = _SCOPE_RE.search(www_authenticate)
        if s:
            scopes = s.group(1).split()
    origin = _origin(server_url)
    path = urlsplit(server_url).path.rstrip("/")
    if path:
        meta_urls.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    meta_urls.append(f"{origin}/.well-known/oauth-protected-resource")

    issuers: list[str] = []
    for url in meta_urls:
        pr = _get_json(http, url)
        if pr:
            if isinstance(pr.get("resource"), str):
                resource = pr["resource"]
            srv = pr.get("authorization_servers")
            if isinstance(srv, list):
                issuers.extend(str(x) for x in srv if isinstance(x, str))
            if not scopes and isinstance(pr.get("scopes_supported"), list):
                scopes = [str(x) for x in pr["scopes_supported"]]
            break
    if not issuers:
        issuers = [origin]  # spec fallback: the resource server is its own AS

    for issuer in issuers:
        iss = issuer.rstrip("/")
        ipath = urlsplit(iss).path
        iorigin = _origin(iss)
        candidates = [
            f"{iorigin}/.well-known/oauth-authorization-server{ipath}",
            f"{iss}/.well-known/oauth-authorization-server",
            f"{iorigin}/.well-known/openid-configuration{ipath}",
            f"{iss}/.well-known/openid-configuration",
        ]
        seen: set[str] = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            am = _get_json(http, url)
            if am and am.get("authorization_endpoint") and am.get("token_endpoint"):
                methods = am.get("code_challenge_methods_supported")
                if isinstance(methods, list) and methods and "S256" not in methods:
                    raise OAuthError(
                        f"authorization server {iss} não suporta PKCE S256 (obrigatório no MCP)."
                    )
                return {
                    "issuer": iss,
                    "authorization_endpoint": str(am["authorization_endpoint"]),
                    "token_endpoint": str(am["token_endpoint"]),
                    "registration_endpoint": (
                        str(am["registration_endpoint"]) if am.get("registration_endpoint") else None
                    ),
                    "resource": resource,
                    "scopes": scopes or [
                        str(x) for x in (am.get("scopes_supported") or []) if isinstance(x, str)
                    ],
                }
    raise OAuthError(
        f"não encontrei metadata OAuth para {server_url} (nem resource metadata, "
        "nem /.well-known/oauth-authorization-server). O servidor suporta OAuth?"
    )


# ── registration ─────────────────────────────────────────────────────


def register_client(meta: dict, http: httpx.Client, redirect_uri: str) -> str:
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise OAuthError(
            "o servidor não oferece registro dinâmico de cliente; configure "
            '"client_id" na entrada do mcp.json.'
        )
    body = {
        "client_name": _CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        r = http.post(endpoint, json=body, timeout=_HTTP_TIMEOUT)
    except httpx.HTTPError as e:
        raise OAuthError(f"registro de cliente falhou: {e}") from e
    if r.status_code not in (200, 201):
        raise OAuthError(f"registro de cliente falhou: HTTP {r.status_code} {r.text[:200]}")
    try:
        cid = r.json().get("client_id")
    except ValueError:
        cid = None
    if not isinstance(cid, str) or not cid:
        raise OAuthError("registro de cliente não devolveu client_id.")
    return cid


# ── PKCE + loopback callback ─────────────────────────────────────────


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackServer:
    """One-shot loopback HTTP listener that captures `code` + `state`."""

    def __init__(self, expected_state: str):
        self.expected_state = expected_state
        self.result: dict = {}
        self._event = threading.Event()
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                ok = q.get("state") == outer.expected_state and "code" in q
                if ok:
                    outer.result = {"code": q["code"]}
                    msg = "Alpha: autenticação concluída. Pode fechar esta aba."
                else:
                    outer.result = {"error": q.get("error") or "state inválido ou sem code"}
                    msg = "Alpha: falha na autenticação — volte ao terminal."
                body = msg.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                outer._event.set()

        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> dict:
        self._event.wait(timeout)
        return self.result

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass


def _exchange(http: httpx.Client, token_endpoint: str, data: dict) -> dict:
    try:
        r = http.post(
            token_endpoint, data=data, timeout=_HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as e:
        raise OAuthError(f"token endpoint falhou: {e}") from e
    if r.status_code != 200:
        raise OAuthError(f"token endpoint HTTP {r.status_code}: {r.text[:200]}")
    try:
        tok = r.json()
    except ValueError as e:
        raise OAuthError("token endpoint devolveu não-JSON") from e
    if not isinstance(tok, dict) or not tok.get("access_token"):
        raise OAuthError("token endpoint não devolveu access_token")
    return tok


def _entry_from_token(tok: dict, meta: dict, client_id: str, old: dict | None = None) -> dict:
    entry = dict(old or {})
    entry.update({
        "access_token": str(tok["access_token"]),
        "token_type": str(tok.get("token_type", "Bearer")),
        "client_id": client_id,
        "token_endpoint": meta["token_endpoint"],
        "authorization_endpoint": meta["authorization_endpoint"],
        "resource": meta.get("resource"),
        "obtained_at": time.time(),
    })
    if tok.get("refresh_token"):
        entry["refresh_token"] = str(tok["refresh_token"])
    if isinstance(tok.get("expires_in"), (int, float)):
        entry["expires_at"] = time.time() + float(tok["expires_in"])
    else:
        entry.pop("expires_at", None)
    return entry


def authorize(
    server_url: str,
    meta: dict,
    http: httpx.Client,
    *,
    client_id: str | None = None,
    scopes: list[str] | None = None,
    opener: Callable[[str], object] = webbrowser.open,
    timeout: float = AUTH_TIMEOUT_S,
    notify: Callable[[str], None] = print,
) -> dict:
    """Run the interactive authorization-code + PKCE flow and persist the
    token for `server_url`. Returns the stored entry."""
    cb = _CallbackServer(expected_state=secrets.token_urlsafe(24))
    cb.start()
    try:
        cid = client_id or register_client(meta, http, cb.redirect_uri)
        verifier, challenge = _pkce_pair()
        params = {
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": cb.redirect_uri,
            "state": cb.expected_state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if meta.get("resource"):
            params["resource"] = meta["resource"]
        use_scopes = scopes if scopes is not None else meta.get("scopes") or []
        if use_scopes:
            params["scope"] = " ".join(use_scopes)
        url = f"{meta['authorization_endpoint']}?{urlencode(params)}"
        notify(f"  Abrindo o navegador para autorizar o MCP…\n  Se não abrir, acesse:\n  {url}")
        try:
            opener(url)
        except Exception as e:  # browser missing — the URL was printed
            logger.debug("webbrowser.open failed: %s", e)
        result = cb.wait(timeout)
        if not result:
            raise OAuthError(f"tempo esgotado ({int(timeout)}s) aguardando o callback do navegador.")
        if "error" in result:
            raise OAuthError(f"autorização negada: {result['error']}")
        data = {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": cb.redirect_uri,
            "client_id": cid,
            "code_verifier": verifier,
        }
        if meta.get("resource"):
            data["resource"] = meta["resource"]
        tok = _exchange(http, meta["token_endpoint"], data)
        entry = _entry_from_token(tok, meta, cid)
        put_stored(server_url, entry)
        return entry
    finally:
        cb.stop()


def refresh(server_url: str, entry: dict, http: httpx.Client) -> dict | None:
    """Refresh an expired token; returns the new entry or None on failure."""
    rt = entry.get("refresh_token")
    if not rt or not entry.get("token_endpoint") or not entry.get("client_id"):
        return None
    data = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": entry["client_id"],
    }
    if entry.get("resource"):
        data["resource"] = entry["resource"]
    try:
        tok = _exchange(http, entry["token_endpoint"], data)
    except OAuthError as e:
        logger.warning("MCP oauth refresh for %s failed: %s", server_url, e)
        return None
    meta = {
        "token_endpoint": entry["token_endpoint"],
        "authorization_endpoint": entry.get("authorization_endpoint", ""),
        "resource": entry.get("resource"),
    }
    new = _entry_from_token(tok, meta, entry["client_id"], old=entry)
    put_stored(server_url, new)
    return new


def cached_access_token(server_url: str, http: httpx.Client | None = None) -> str | None:
    """Stored token for `server_url`, refreshed if expired and refreshable.
    Never interactive."""
    entry = get_stored(server_url)
    if not entry:
        return None
    if _is_expired(entry):
        if http is None:
            return None
        entry = refresh(server_url, entry, http)
        if not entry:
            return None
    return entry.get("access_token")


def interactive_allowed() -> bool:
    import sys

    if os.environ.get("ALPHA_MCP_OAUTH", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    if os.environ.get("ALPHA_SUBAGENT_HEADLESS") == "1" or os.environ.get("ALPHA_DELEGATED") == "1":
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def login(
    server_url: str,
    http: httpx.Client | None = None,
    *,
    www_authenticate: str | None = None,
    client_id: str | None = None,
    opener: Callable[[str], object] = webbrowser.open,
    notify: Callable[[str], None] = print,
) -> dict:
    """Full interactive login for `server_url`: discover → (register) →
    authorize → store. Returns the stored entry."""
    own = http is None
    http = http or httpx.Client(follow_redirects=False)
    try:
        meta = discover(server_url, http, www_authenticate)
        stored = get_stored(server_url) or {}
        cid = client_id or stored.get("client_id")
        return authorize(server_url, meta, http, client_id=cid, opener=opener, notify=notify)
    finally:
        if own:
            http.close()
