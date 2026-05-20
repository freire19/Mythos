"""Browser tab management (#DM029 split).

Tab CRUD operations extracted from `browser_tools.py`. The session
singleton and validation helpers (`_check_available`, `_domain_allowed`,
`_ensure_session`) stay in browser_tools.py because most other tools use
them too — they're re-imported here so the tab handlers don't grow a
parallel set of shims.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def _browser_list_tabs() -> dict:
    from .browser_tools import _check_available
    from .browser_session import BrowserSession

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
        except Exception as e:
            tabs.append({
                "index": i,
                "url": getattr(p, "url", "?"),
                "active": i == session.active_idx,
                "error": f"{type(e).__name__}: {e}",
            })
    return {"tabs": tabs, "count": len(tabs)}


async def _browser_new_tab(url: str | None = None) -> dict:
    from .browser_tools import _check_available, _domain_allowed, _ensure_session
    from .browser_session import validate_browser_url

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
    from .browser_tools import _check_available
    from .browser_session import BrowserSession

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
    from .browser_tools import _check_available
    from .browser_session import BrowserSession

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
    close_error: str | None = None
    try:
        await page.close()
    except Exception as e:
        # pop() runs even on failure — session state must advance.
        close_error = f"{type(e).__name__}: {e}"
        logger.warning("browser_close_tab: page.close() failed: %s", close_error)
    session.pages.pop(index)
    if not session.pages:
        await session.close()
        result: dict = {"closed": index, "session_closed": True}
    else:
        # Aba fechada antes da ativa desloca o índice ativo uma posição à esquerda
        if index < session.active_idx:
            session.active_idx -= 1
        session.active_idx = max(0, min(session.active_idx, len(session.pages) - 1))
        result = {"closed": index, "tab_count": len(session.pages), "active_tab": session.active_idx}
    if close_error:
        result["close_error"] = close_error
    return result
