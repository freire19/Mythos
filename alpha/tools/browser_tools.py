"""Browser automation tools (Playwright).

Persistent browser session shared across calls. Read-only operations
(navigate, get_content, screenshot) are SAFE; interaction (click, fill,
execute_js) is DESTRUCTIVE and requires user approval.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from .browser_session import (
    PLAYWRIGHT_AVAILABLE,
    PLAYWRIGHT_INSTALL_MSG,
    BrowserSession,
    cdp_endpoint,
    validate_browser_url,
)

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 50_000
_MAX_QUERY_RESULTS = 50
_MAX_DESCRIBE_ELEMENTS = 100


def _check_available() -> dict | None:
    if not PLAYWRIGHT_AVAILABLE:
        return {"error": PLAYWRIGHT_INSTALL_MSG}
    return None


def _compile_user_regex(pattern: str):
    """Compila regex vinda do usuário → (rx, error_dict_or_None) (#DM077)."""
    import re

    try:
        return re.compile(pattern), None
    except re.error as e:
        return None, {"error": f"Regex inválida: {e}"}


def _domain_allowed(url: str) -> str | None:
    from ..config import (
        BROWSER_DOMAIN_ALLOWLIST,
        BROWSER_DOMAIN_BLOCKLIST,
        BROWSER_REQUIRE_ALLOWLIST,
    )

    host = (urlparse(url).hostname or "").lower()
    for blocked in BROWSER_DOMAIN_BLOCKLIST:
        if host == blocked or host.endswith("." + blocked):
            return f"Domínio '{host}' está na blocklist"
    if BROWSER_DOMAIN_ALLOWLIST:
        for allowed in BROWSER_DOMAIN_ALLOWLIST:
            if host == allowed or host.endswith("." + allowed):
                return None
        return f"Domínio '{host}' fora da allowlist"
    # Allowlist vazia: fail-closed se o operador exigir allowlist explicita.
    if BROWSER_REQUIRE_ALLOWLIST:
        return (
            "Allowlist vazia e ALPHA_BROWSER_REQUIRE_ALLOWLIST=1: defina "
            "ALPHA_BROWSER_ALLOWLIST=dominio1,dominio2 antes de navegar."
        )
    return None


async def _ensure_session(headless: bool = True):
    session = await BrowserSession.get()
    if not session.is_open():
        # Lazy open honours the user's attach opt-in too, so browser_navigate
        # before browser_open lands in their Chrome, not a fresh Chromium.
        await session.open(headless=headless, profile=_profile_default(), cdp=cdp_endpoint())
    return session


async def _require_page():
    """Returns (page, error_dict_or_None)."""
    err = _check_available()
    if err:
        return None, err
    session = await BrowserSession.get()
    if not session.is_open():
        return None, {"error": "Sessão de navegador não aberta. Use browser_open primeiro."}
    page = session.page
    if page is None:
        return None, {"error": "Sem aba ativa na sessão."}
    return page, None


async def _require_session():
    """Returns (session, error_dict_or_None) — para tools que operam na
    sessão (buffers, abas) em vez da página ativa. Definida antes do import
    de `_browser_tabs` (abaixo) para que os tab tools a reutilizem (#DM077)."""
    err = _check_available()
    if err:
        return None, err
    session = await BrowserSession.get()
    if not session.is_open():
        return None, {"error": "Sessão de navegador não aberta. Use browser_open primeiro."}
    return session, None


# ─── Session lifecycle ───────────────────────────────────────────


def _profile_default() -> bool:
    """ALPHA_BROWSER_PROFILE=1 liga o modo perfil por default (opt-in)."""
    import os

    return os.environ.get("ALPHA_BROWSER_PROFILE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def _browser_open(headless: bool = True, profile: bool | None = None) -> dict:
    err = _check_available()
    if err:
        return err
    if profile is None:
        profile = _profile_default()
    try:
        # Attach is the USER's opt-in only (env var) — no model-controlled
        # endpoint on this auto-approved tool.
        endpoint = cdp_endpoint()
    except ValueError as e:
        return {"error": str(e)}
    try:
        session = await BrowserSession.get()
        already_open = session.is_open()
        same_mode = already_open and session.same_mode(headless, profile, endpoint)
        if not same_mode:
            await session.open(headless=headless, profile=profile, cdp=endpoint)
        result = {
            "status": "already_open" if same_mode else ("reopened" if already_open else "opened"),
            "headless": session.headless,
            "profile": session.persistent,
            "attached": session.attached,
            "tab_count": len(session.pages),
        }
        if session.profile_dir:
            result["profile_dir"] = session.profile_dir
        if session.attached:
            result["cdp"] = session.cdp
            result["note"] = (
                "Attached to the user's own Chrome (their cookies/logins apply). You work "
                "only in tabs you open; the user's tabs are not visible to you. "
                "browser_close closes your tabs and disconnects."
            )
        return result
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _browser_close() -> dict:
    err = _check_available()
    if err:
        return err
    session = await BrowserSession.get()
    if not session.is_open():
        return {"status": "not_open"}
    await session.close()
    return {"status": "closed"}


async def _browser_status() -> dict:
    err = _check_available()
    if err:
        return err
    session = await BrowserSession.get()
    if not session.is_open():
        return {"open": False}
    page = session.page
    return {
        "open": True,
        "url": page.url if page else "",
        "title": (await page.title()) if page else "",
        "tab_count": len(session.pages),
        "active_tab": session.active_idx,
        "headless": session.headless,
        "profile": session.persistent,
        "attached": session.attached,
        "cdp": session.cdp,
    }


# ─── Navigation ──────────────────────────────────────────────────


async def _browser_navigate(url: str, wait_until: str = "load", timeout: int = 30) -> dict:
    err = _check_available()
    if err:
        return err
    url_err = validate_browser_url(url) or _domain_allowed(url)
    if url_err:
        return {"error": url_err, "blocked": True}
    try:
        session = await _ensure_session()
        page = session.page
        if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
            wait_until = "load"
        resp = await page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
        return {
            "url": page.url,
            "status_code": resp.status if resp else None,
            "title": await page.title(),
        }
    except Exception as e:
        return {"error": f"Navegação falhou: {type(e).__name__}: {e}"}


async def _history_nav(page, go) -> dict:
    """#D137: run a history navigation (go_back/go_forward) and RE-VALIDATE the
    landed URL. back/forward can reach a URL never checked directly — e.g. one
    pushed via history.pushState by malicious JS, or a `file://`/internal IP —
    bypassing the SSRF/scheme checks `_browser_navigate` applies. Rescue to
    about:blank if the landed URL is blocked.
    """
    await go()
    landed = page.url
    if landed and not landed.startswith(("about:", "chrome:", "data:")):
        url_err = validate_browser_url(landed) or _domain_allowed(landed)
        if url_err:
            try:
                await page.goto("about:blank")
            except Exception as e:
                logger.warning("history-nav rescue to about:blank failed: %s", e)
            return {"error": f"Navegação bloqueada: {url_err}", "blocked": True}
    return {"url": page.url, "title": await page.title()}


async def _browser_back() -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        return await _history_nav(page, page.go_back)
    except Exception as e:
        return {"error": str(e)}


async def _browser_forward() -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        return await _history_nav(page, page.go_forward)
    except Exception as e:
        return {"error": str(e)}


async def _browser_reload() -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.reload()
        return {"url": page.url, "title": await page.title()}
    except Exception as e:
        return {"error": str(e)}


# ─── Reading ─────────────────────────────────────────────────────


async def _browser_get_content(format: str = "text") -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        if format == "html":
            content = await page.content()
        else:
            content = await page.inner_text("body")
        truncated = len(content) > _MAX_CONTENT_CHARS
        if truncated:
            content = content[:_MAX_CONTENT_CHARS]
        return {
            "url": page.url,
            "title": await page.title(),
            "format": format,
            "content": content,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


async def _browser_screenshot(save_to: str | None = None, full_page: bool = False) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        # Usar a fonte canonical (`tools.workspace.AGENT_WORKSPACE`, Path
        # resolvida com forbidden-system-dir guard), nao a string vazia de
        # `config.AGENT_WORKSPACE` que esta sendo removida (#D021-BUGS).
        from .workspace import AGENT_WORKSPACE

        if not save_to:
            save_to = f"browser_screenshot_{int(time.time())}.png"

        # Validar workspace mesmo quando save_to e absoluto. Antes,
        # `path.is_absolute()` pulava a validacao e o modelo podia escrever
        # em /etc/cron.d/foo.png ou ~/.ssh/known_hosts.png sobrescrevendo
        # arquivos pessoais (#D105-SEC).
        ws = Path(AGENT_WORKSPACE).resolve()
        path = Path(save_to).expanduser()
        if not path.is_absolute():
            path = ws / path
        path = path.resolve()
        try:
            path.relative_to(ws)
        except ValueError:
            return {"error": f"save_to fora do workspace permitido ({ws})"}

        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=full_page)
        viewport = getattr(page, "viewport_size", None) or {}
        return {
            "saved_to": str(path),
            "size_bytes": path.stat().st_size,
            "url": page.url,
            # Viewport para o modelo calibrar coordenadas (Plano-Browser E3).
            "viewport": {"width": viewport.get("width"), "height": viewport.get("height")},
            # Chave _-prefixada: oculta do JSON textual enviado ao LLM
            # (_format_result). O executor a usa para anexar a imagem como
            # mensagem de visão quando o provider ativo suporta (Etapa 1).
            "_image_path": str(path),
        }
    except Exception as e:
        return {"error": str(e)}


_DESCRIBE_JS = """(maxItems) => {
    const sel = 'a, button, input, select, textarea, [role=button], [role=link], [role=textbox], [role=combobox], [role=menuitem]';
    const items = [];
    let count = 0;
    document.querySelectorAll(sel).forEach((el) => {
        if (count >= maxItems) return;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;
        const tag = el.tagName.toLowerCase();
        let selector = '';
        if (el.id) selector = '#' + CSS.escape(el.id);
        else if (el.name) selector = `${tag}[name="${el.name}"]`;
        else if (el.getAttribute('data-testid')) selector = `[data-testid="${el.getAttribute('data-testid')}"]`;
        items.push({
            tag,
            type: el.type || '',
            name: el.name || '',
            id: el.id || '',
            aria_label: el.getAttribute('aria-label') || '',
            placeholder: el.placeholder || '',
            text: (el.innerText || el.value || '').slice(0, 80).trim(),
            href: el.href || '',
            selector,
        });
        count++;
    });
    return items;
}"""


async def _browser_describe_page() -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        elements = await page.evaluate(_DESCRIBE_JS, _MAX_DESCRIBE_ELEMENTS)
        return {
            "url": page.url,
            "title": await page.title(),
            "elements": elements,
            "count": len(elements),
        }
    except Exception as e:
        return {"error": str(e)}


async def _browser_query(selector: str, attribute: str | None = None) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        elements = await page.query_selector_all(selector)
        if not elements:
            return {"selector": selector, "matches": [], "count": 0}
        results = []
        for el in elements[:_MAX_QUERY_RESULTS]:
            entry = {
                "text": (await el.inner_text())[:300],
                "visible": await el.is_visible(),
            }
            if attribute:
                entry[attribute] = await el.get_attribute(attribute)
            results.append(entry)
        return {"selector": selector, "matches": results, "count": len(elements)}
    except Exception as e:
        return {"error": str(e)}


async def _browser_wait_for(selector: str, timeout: int = 10) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.wait_for_selector(selector, timeout=timeout * 1000)
        return {"selector": selector, "found": True}
    except Exception as e:
        return {"selector": selector, "found": False, "error": str(e)}


# ─── Tab management ──────────────────────────────────────────────


# Tab management (#DM029) lives in `_browser_tabs.py` to keep this file
# focused on session lifecycle, navigation, content extraction, and
# interaction. Re-imported here so `_browser_registrations.py` can still
# pick them up by module reference.
from ._browser_tabs import (  # noqa: E402,F401 — re-export (E402: after _check_available; F401: AUDIT_V2.2 #024 intentional re-export for _browser_registrations)
    _browser_close_tab,
    _browser_list_tabs,
    _browser_new_tab,
    _browser_switch_tab,
)


# ─── Interaction (DESTRUCTIVE) ───────────────────────────────────


async def _browser_click(selector: str, timeout: int = 10) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.click(selector, timeout=timeout * 1000)
        return {"selector": selector, "clicked": True, "url": page.url}
    except Exception as e:
        return {"error": str(e)}


async def _browser_fill(selector: str, value: str, timeout: int = 10) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.fill(selector, value, timeout=timeout * 1000)
        return {"selector": selector, "filled": True, "length": len(value)}
    except Exception as e:
        return {"error": str(e)}


async def _browser_select_option(selector: str, value: str, timeout: int = 10) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        result = await page.select_option(selector, value=value, timeout=timeout * 1000)
        return {"selector": selector, "selected": result}
    except Exception as e:
        return {"error": str(e)}


async def _browser_press_key(key: str, selector: str | None = None) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        if selector:
            await page.press(selector, key)
        else:
            await page.keyboard.press(key)
        return {"key": key, "pressed": True}
    except Exception as e:
        return {"error": str(e)}


async def _browser_execute_js(code: str) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        result = await page.evaluate(code)
        try:
            import json

            json.dumps(result)
            return {"result": result}
        except (TypeError, ValueError):
            return {"result": str(result)}
    except Exception as e:
        return {"error": str(e)}


# ─── Computer-use interactions (Plano-Browser Etapa 3) ───────────
#
# Coordenadas são do VIEWPORT do último browser_screenshot (que retorna
# `viewport` justamente para o modelo calibrar).


async def _browser_click_at(
    x: float, y: float, button: str = "left", click_count: int = 1
) -> dict:
    page, err = await _require_page()
    if err:
        return err
    if button not in ("left", "right", "middle"):
        return {"error": f"Botão inválido: {button} (left|right|middle)"}
    try:
        await page.mouse.click(x, y, button=button, click_count=min(max(click_count, 1), 3))
        return {"clicked_at": [x, y], "button": button, "url": page.url}
    except Exception as e:
        return {"error": str(e)}


async def _browser_scroll(
    direction: str = "down", amount_px: int = 600, selector: str | None = None
) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        if selector:
            await page.locator(selector).first.scroll_into_view_if_needed(timeout=5000)
            return {"scrolled_to": selector}
        amount = min(max(int(amount_px), 40), 20_000)
        dx, dy = {
            "down": (0, amount),
            "up": (0, -amount),
            "right": (amount, 0),
            "left": (-amount, 0),
        }.get(direction, (None, None))
        if dy is None and dx is None:
            return {"error": f"Direção inválida: {direction} (up|down|left|right)"}
        await page.mouse.wheel(dx, dy)
        return {"scrolled": direction, "amount_px": amount}
    except Exception as e:
        return {"error": str(e)}


async def _browser_type(text: str, delay_ms: int = 20) -> dict:
    """Digitação char a char no elemento focado — para editores rich-text e
    inputs que rejeitam o set direto do `browser_fill`."""
    page, err = await _require_page()
    if err:
        return err
    if len(text) > 5000:
        return {"error": "Texto > 5000 chars — use browser_fill para conteúdo longo"}
    try:
        await page.keyboard.type(text, delay=min(max(delay_ms, 0), 200))
        return {"typed_chars": len(text)}
    except Exception as e:
        return {"error": str(e)}


async def _browser_hover(
    selector: str | None = None, x: float | None = None, y: float | None = None
) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        if selector:
            await page.hover(selector, timeout=5000)
            return {"hovered": selector}
        if x is not None and y is not None:
            await page.mouse.move(x, y)
            return {"hovered_at": [x, y]}
        return {"error": "Informe selector OU x+y"}
    except Exception as e:
        return {"error": str(e)}


async def _browser_drag(
    from_x: float, from_y: float, to_x: float, to_y: float, steps: int = 12
) -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.mouse.move(from_x, from_y)
        await page.mouse.down()
        await page.mouse.move(to_x, to_y, steps=min(max(steps, 1), 50))
        await page.mouse.up()
        return {"dragged": {"from": [from_x, from_y], "to": [to_x, to_y]}}
    except Exception as e:
        return {"error": str(e)}


# ─── Observability (Plano-Browser Etapa 4) ───────────────────────


def _ts_fmt(ts: float) -> str:
    import time as _time

    try:
        return _time.strftime("%H:%M:%S", _time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return "?"


async def _browser_read_console(
    pattern: str | None = None, level: str | None = None, limit: int = 50
) -> dict:
    session, err = await _require_session()
    if err:
        return err
    entries = list(session.console_logs)
    if level:
        entries = [e for e in entries if e.get("level") == level]
    if pattern:
        rx, rx_err = _compile_user_regex(pattern)
        if rx_err:
            return rx_err
        entries = [e for e in entries if rx.search(e.get("text", ""))]
    limit = min(max(limit, 1), 200)
    tail = entries[-limit:]
    return {
        "entries": [
            {"time": _ts_fmt(e["ts"]), "level": e.get("level"), "text": e.get("text")}
            for e in tail
        ],
        "matched": len(entries),
        "returned": len(tail),
    }


async def _browser_read_network(
    url_pattern: str | None = None, status: int | None = None, limit: int = 50
) -> dict:
    session, err = await _require_session()
    if err:
        return err
    entries = list(session.network_events)
    if status is not None:
        entries = [e for e in entries if e.get("status") == status]
    if url_pattern:
        rx, rx_err = _compile_user_regex(url_pattern)
        if rx_err:
            return rx_err
        entries = [e for e in entries if rx.search(e.get("url", ""))]
    limit = min(max(limit, 1), 200)
    tail = entries[-limit:]
    return {
        "entries": [
            {
                "time": _ts_fmt(e["ts"]),
                "method": e.get("method"),
                "status": e.get("status"),
                "url": e.get("url"),
                **({"failure": e["failure"]} if e.get("failure") else {}),
                **({"content_type": e["content_type"]} if e.get("content_type") else {}),
            }
            for e in tail
        ],
        "matched": len(entries),
        "returned": len(tail),
    }


# ─── Complementos (Plano-Browser Etapa 5) ────────────────────────


async def _browser_upload(selector: str, path: str) -> dict:
    """Anexa um arquivo do WORKSPACE a um <input type=file>."""
    page, err = await _require_page()
    if err:
        return err
    from .workspace import AGENT_WORKSPACE, assert_within_workspace

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(AGENT_WORKSPACE) / p
    try:
        p = p.resolve()
    except OSError as e:
        return {"error": f"Path inválido: {e}"}
    ws_err = assert_within_workspace(p)
    if ws_err:
        return {"error": ws_err}
    if not p.is_file():
        return {"error": f"Arquivo não encontrado: {p}"}
    try:
        await page.set_input_files(selector, str(p), timeout=10_000)
        return {"uploaded": str(p), "selector": selector}
    except Exception as e:
        return {"error": str(e)}


async def _browser_wait_for_navigation(
    url_pattern: str | None = None,
    state: str = "load",
    timeout: int = 15,
) -> dict:
    """Espera a página atingir um load state e/ou uma URL (regex)."""
    page, err = await _require_page()
    if err:
        return err
    if state not in ("load", "domcontentloaded", "networkidle"):
        return {"error": f"State inválido: {state}"}
    timeout_ms = min(max(timeout, 1), 60) * 1000
    try:
        if url_pattern:
            rx, rx_err = _compile_user_regex(url_pattern)
            if rx_err:
                return rx_err
            await page.wait_for_url(rx, wait_until=state, timeout=timeout_ms)
        else:
            await page.wait_for_load_state(state, timeout=timeout_ms)
        return {"url": page.url, "state": state}
    except Exception as e:
        return {"error": str(e), "url": page.url}


async def _browser_aria_snapshot(selector: str = "body") -> dict:
    """Árvore de acessibilidade real (YAML) — visão estrutural precisa da
    página, complementa o describe_page heurístico."""
    page, err = await _require_page()
    if err:
        return err
    try:
        locator = page.locator(selector).first
        if not hasattr(locator, "aria_snapshot"):
            return {
                "error": (
                    "aria_snapshot requer Playwright >= 1.49 — "
                    "pip install -U playwright"
                )
            }
        snapshot = await locator.aria_snapshot()
        truncated = len(snapshot) > 30_000
        return {
            "selector": selector,
            "snapshot": snapshot[:30_000],
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


# ─── Tool registrations ──────────────────────────────────────────

_NO_PARAMS = {"type": "object", "properties": {}}



# Tool registrations live in _browser_registrations.py (split #081)
from . import _browser_registrations  # noqa: F401 — triggers registration
