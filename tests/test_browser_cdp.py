"""Attach ao Chrome do usuário via CDP (`ALPHA_BROWSER_CDP`).

Contratos (revisados após o /code-review de 86a7cfd):
- só o usuário liga o modo (env); `browser_open` não aceita endpoint do
  modelo;
- `cdp_endpoint` normaliza porta/host:porta/url, exige loopback salvo
  `ALPHA_BROWSER_CDP_ALLOW_REMOTE=1`, trata IPv6 com colchetes;
- attach usa o context default do usuário mas abre UMA aba própria e
  NUNCA adota as abas do usuário: sem listener "page" no context, as abas
  dele ficam fora de guard, tab tools e read tools;
- popups de abas do agente e `browser_new_tab` são adotados (com guard);
  downloads nas abas do agente são cancelados;
- `close` fecha só as abas do agente e desconecta;
- `_ensure_session` (open preguiçoso) respeita o env;
- /doctor descreve o modo e avisa endpoint inválido.
"""

from __future__ import annotations

import pytest

import alpha.tools.browser_session as bs
from alpha.tools.browser_session import BrowserSession, cdp_endpoint


class _Page:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False
        self.handlers: dict[str, list] = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    async def title(self):
        return "t"

    async def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False
        self.listeners: list[str] = []

    def on(self, event, fn):
        self.listeners.append(event)

    def remove_listener(self, *a, **k):
        pass

    async def new_page(self):
        p = _Page()
        self.pages.append(p)
        return p

    async def close(self):
        self.closed = True


class _AttachedBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.disconnected = False

    def is_connected(self):
        return not self.disconnected

    async def new_context(self, **kw):
        c = _Ctx([])
        self.contexts.append(c)
        return c

    async def close(self):
        self.disconnected = True


class _LaunchedBrowser(_AttachedBrowser):
    pass


class _Chromium:
    def __init__(self, user_browser):
        self.user_browser = user_browser
        self.cdp_calls: list[str] = []

    async def connect_over_cdp(self, endpoint, **kw):
        self.cdp_calls.append(endpoint)
        return self.user_browser

    async def launch(self, headless=True):
        return _LaunchedBrowser([])

    async def launch_persistent_context(self, user_data_dir, **kw):
        return _Ctx([_Page()])


class _PW:
    def __init__(self, user_browser):
        self.chromium = _Chromium(user_browser)

    async def stop(self):
        pass


@pytest.fixture
def fake(monkeypatch, tmp_path):
    bank, mail = _Page("https://bank.example/"), _Page("https://mail.example/")
    user_ctx = _Ctx([bank, mail])                # the user's two open tabs
    user_browser = _AttachedBrowser([user_ctx])
    pw = _PW(user_browser)

    class _AP:
        async def start(self):
            return pw

    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(bs, "_ensure_playwright_loaded", lambda: True)
    monkeypatch.setattr(bs, "async_playwright", lambda: _AP())
    monkeypatch.setenv("ALPHA_BROWSER_PROFILE_DIR", str(tmp_path / "perfil"))
    monkeypatch.delenv("ALPHA_BROWSER_CDP", raising=False)
    monkeypatch.delenv("ALPHA_BROWSER_CDP_ALLOW_REMOTE", raising=False)
    BrowserSession._instance = None
    yield pw, user_browser, user_ctx, (bank, mail)
    BrowserSession._instance = None


class TestEndpoint:
    @pytest.mark.parametrize("raw,expected", [
        ("9222", "http://127.0.0.1:9222"),
        ("localhost:9222", "http://localhost:9222"),
        ("[::1]:9222", "http://[::1]:9222"),
        ("http://127.0.0.1:9222", "http://127.0.0.1:9222"),
        ("ws://127.0.0.1:9222/devtools/browser/abc", "ws://127.0.0.1:9222/devtools/browser/abc"),
        ("  ", None),
    ])
    def test_normalizes(self, monkeypatch, raw, expected):
        monkeypatch.delenv("ALPHA_BROWSER_CDP_ALLOW_REMOTE", raising=False)
        assert cdp_endpoint(raw) == expected

    def test_env_default(self, monkeypatch):
        monkeypatch.setenv("ALPHA_BROWSER_CDP", "9333")
        assert cdp_endpoint() == "http://127.0.0.1:9333"

    def test_remote_requires_opt_in(self, monkeypatch):
        monkeypatch.delenv("ALPHA_BROWSER_CDP_ALLOW_REMOTE", raising=False)
        with pytest.raises(ValueError, match="não-local"):
            cdp_endpoint("http://10.0.0.5:9222")
        monkeypatch.setenv("ALPHA_BROWSER_CDP_ALLOW_REMOTE", "1")
        assert cdp_endpoint("http://10.0.0.5:9222") == "http://10.0.0.5:9222"

    @pytest.mark.parametrize("raw,msg", [
        ("ftp://127.0.0.1:1", "inválido"),
        ("::1:9222", "colchetes"),
        ("http://127.0.0.1:notaport", "inválido"),
    ])
    def test_rejects(self, raw, msg):
        with pytest.raises(ValueError, match=msg):
            cdp_endpoint(raw)


class TestSession:
    @pytest.mark.asyncio
    async def test_attach_opens_own_tab_and_ignores_user_tabs(self, fake):
        pw, user_browser, user_ctx, (bank, mail) = fake
        s = await BrowserSession.get()
        await s.open(headless=True, profile=True, cdp="http://127.0.0.1:9222")
        assert pw.chromium.cdp_calls == ["http://127.0.0.1:9222"]
        assert s.attached is True and s.persistent is False and s.headless is False
        assert s.browser is user_browser and s.context is user_ctx
        # exactly one tab, created by us — the user's bank/mail tabs are not ours
        assert len(s.pages) == 1 and s.page is user_ctx.pages[-1]
        assert bank not in s.pages and mail not in s.pages
        # no context-wide listener: tabs the user opens later are never adopted
        assert "page" not in user_ctx.listeners
        # the user's tabs got no guard/observability listeners
        assert bank.handlers == {} and mail.handlers == {}
        # our tab is guarded and tracks popups + downloads
        assert {"framenavigated", "popup", "download"} <= set(s.page.handlers)

    @pytest.mark.asyncio
    async def test_popup_from_agent_tab_is_adopted(self, fake):
        s = await BrowserSession.get()
        await s.open(cdp="http://127.0.0.1:9222")
        popup = _Page("https://example.com/popup")
        for fn in s.page.handlers["popup"]:
            fn(popup)
        assert popup in s.pages and "framenavigated" in popup.handlers

    @pytest.mark.asyncio
    async def test_close_closes_only_agent_tabs(self, fake):
        pw, user_browser, user_ctx, (bank, mail) = fake
        s = await BrowserSession.get()
        await s.open(cdp="http://127.0.0.1:9222")
        mine = s.page
        await s.close()
        assert mine.closed is True
        assert bank.closed is False and mail.closed is False
        assert user_browser.disconnected is True and user_ctx.closed is False
        assert BrowserSession._instance is None

    @pytest.mark.asyncio
    async def test_same_mode_and_switch(self, fake):
        pw, user_browser, *_ = fake
        s = await BrowserSession.get()
        await s.open(cdp="http://127.0.0.1:9222")
        await s.open(cdp="http://127.0.0.1:9222")  # no-op
        assert pw.chromium.cdp_calls == ["http://127.0.0.1:9222"]
        await s.open(headless=True)  # back to a launched browser
        assert s.attached is False and user_browser.disconnected is True
        assert isinstance(s.browser, _LaunchedBrowser)


class TestTools:
    @pytest.mark.asyncio
    async def test_open_reads_env_only(self, fake, monkeypatch):
        import alpha.tools.browser_tools as bt

        monkeypatch.setattr(bt, "PLAYWRIGHT_AVAILABLE", True)
        r = await bt._browser_open()  # no env → launched browser
        assert r["attached"] is False
        monkeypatch.setenv("ALPHA_BROWSER_CDP", "9222")
        r = await bt._browser_open()
        assert r["status"] == "reopened" and r["attached"] is True and r["tab_count"] == 1
        assert r["cdp"] == "http://127.0.0.1:9222" and "not visible to you" in r["note"]
        st = await bt._browser_status()
        assert st["attached"] is True and st["tab_count"] == 1

    @pytest.mark.asyncio
    async def test_open_rejects_bad_env(self, fake, monkeypatch):
        import alpha.tools.browser_tools as bt

        monkeypatch.setattr(bt, "PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setenv("ALPHA_BROWSER_CDP", "http://evil.example:9222")
        r = await bt._browser_open()
        assert "error" in r and "não-local" in r["error"]

    @pytest.mark.asyncio
    async def test_lazy_open_honours_env(self, fake, monkeypatch):
        import alpha.tools.browser_tools as bt

        monkeypatch.setenv("ALPHA_BROWSER_CDP", "9222")
        s = await bt._ensure_session()
        assert s.attached is True

    @pytest.mark.asyncio
    async def test_new_tab_and_close_tab_stay_on_agent_tabs(self, fake, monkeypatch):
        import alpha.tools._browser_tabs as tabs
        import alpha.tools.browser_tools as bt

        pw, user_browser, user_ctx, (bank, mail) = fake
        monkeypatch.setattr(bt, "PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setenv("ALPHA_BROWSER_CDP", "9222")
        await bt._browser_open()
        r = await tabs._browser_new_tab()
        assert r["tab_count"] == 2
        s = await BrowserSession.get()
        assert "framenavigated" in s.pages[1].handlers  # guard wired on the new tab
        for _ in range(3):
            await tabs._browser_close_tab(index=0)
        assert bank.closed is False and mail.closed is False

    def test_schema_has_no_model_endpoint(self):
        from alpha.tools import get_tool, load_all_tools

        load_all_tools()
        td = get_tool("browser_open")
        assert "cdp" not in td.parameters["properties"]
        assert "ALPHA_BROWSER_CDP" in td.description


class TestDoctor:
    def test_doctor_mentions_attach(self, monkeypatch):
        from alpha.cli.commands import doctor_cmd

        monkeypatch.setattr(doctor_cmd.importlib.util, "find_spec", lambda n: object())
        monkeypatch.setattr("alpha.config.browser_tools_enabled", lambda: True)
        monkeypatch.setenv("ALPHA_BROWSER_CDP", "9222")
        monkeypatch.setenv("ALPHA_BROWSER_ALLOWLIST", "example.com")
        status, _, detail = doctor_cmd._playwright()
        assert status == "ok" and "attach ao Chrome em http://127.0.0.1:9222" in detail
        monkeypatch.setenv("ALPHA_BROWSER_CDP", "http://10.1.1.1:9222")
        status, _, detail = doctor_cmd._playwright()
        assert status == "warn" and "não-local" in detail
