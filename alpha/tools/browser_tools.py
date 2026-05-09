"""Browser automation tools (Playwright).

Persistent browser session shared across calls. Read-only operations
(navigate, get_content, screenshot) are SAFE; interaction (click, fill,
execute_js) is DESTRUCTIVE and requires user approval.
"""

import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from .browser_session import (
    PLAYWRIGHT_AVAILABLE,
    BrowserSession,
    validate_browser_url,
)

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 50_000
_MAX_QUERY_RESULTS = 50
_MAX_DESCRIBE_ELEMENTS = 100


def _check_available() -> dict | None:
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "error": (
                "Playwright not installed. Run: "
                "pip install playwright && playwright install chromium"
            )
        }
    return None


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
        await session.open(headless=headless)
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


# ─── Session lifecycle ───────────────────────────────────────────


async def _browser_open(headless: bool = True) -> dict:
    err = _check_available()
    if err:
        return err
    try:
        session = await BrowserSession.get()
        already_open = session.is_open()
        if not already_open:
            await session.open(headless=headless)
        return {
            "status": "already_open" if already_open else "opened",
            "headless": session.headless,
            "tab_count": len(session.pages),
        }
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


async def _browser_back() -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.go_back()
        return {"url": page.url, "title": await page.title()}
    except Exception as e:
        return {"error": str(e)}


async def _browser_forward() -> dict:
    page, err = await _require_page()
    if err:
        return err
    try:
        await page.go_forward()
        return {"url": page.url, "title": await page.title()}
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
        return {
            "saved_to": str(path),
            "size_bytes": path.stat().st_size,
            "url": page.url,
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


async def _browser_list_tabs() -> dict:
    err = _check_available()
    if err:
        return err
    session = await BrowserSession.get()
    if not session.is_open():
        return {"error": "Sessão não aberta."}
    tabs = []
    for i, p in enumerate(session.pages):
        try:
            tabs.append({
                "index": i,
                "url": p.url,
                "title": await p.title(),
                "active": i == session.active_idx,
            })
        except Exception:
            continue
    return {"tabs": tabs, "count": len(tabs)}


async def _browser_new_tab(url: str | None = None) -> dict:
    err = _check_available()
    if err:
        return err
    if url:
        url_err = validate_browser_url(url) or _domain_allowed(url)
        if url_err:
            return {"error": url_err, "blocked": True}
    try:
        session = await _ensure_session()
        page = await session.context.new_page()
        if page not in session.pages:
            session.pages.append(page)
        session.active_idx = session.pages.index(page)
        result = {"index": session.active_idx, "tab_count": len(session.pages)}
        if url:
            await page.goto(url, timeout=30000)
            result["url"] = page.url
            result["title"] = await page.title()
        return result
    except Exception as e:
        return {"error": str(e)}


async def _browser_switch_tab(index: int) -> dict:
    err = _check_available()
    if err:
        return err
    session = await BrowserSession.get()
    if not session.is_open():
        return {"error": "Sessão não aberta."}
    if index < 0 or index >= len(session.pages):
        return {"error": f"Índice inválido {index}. Faixa: 0..{len(session.pages) - 1}"}
    session.active_idx = index
    page = session.pages[index]
    try:
        await page.bring_to_front()
        return {"index": index, "url": page.url, "title": await page.title()}
    except Exception as e:
        return {"error": str(e)}


async def _browser_close_tab(index: int | None = None) -> dict:
    err = _check_available()
    if err:
        return err
    session = await BrowserSession.get()
    if not session.is_open():
        return {"error": "Sessão não aberta."}
    if index is None:
        index = session.active_idx
    if index < 0 or index >= len(session.pages):
        return {"error": f"Índice inválido {index}"}
    page = session.pages[index]
    try:
        await page.close()
    except Exception:
        pass
    session.pages.pop(index)
    if not session.pages:
        await session.close()
        return {"closed": index, "session_closed": True}
    session.active_idx = max(0, min(session.active_idx, len(session.pages) - 1))
    return {"closed": index, "tab_count": len(session.pages), "active_tab": session.active_idx}


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


# Trigger registrations (extracted to _browser_registrations.py — #081)
from . import _browser_registrations  # noqa: F401

