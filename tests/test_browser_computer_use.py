"""Browser Etapas 3+4 — computer-use (coordenadas) e observabilidade.

Sem Chromium: páginas/mouse falsos exercitam a lógica das tools; os ring
buffers da sessão são alimentados invocando os listeners diretamente.
"""

import pytest

import alpha.tools.browser_tools as bt
from alpha.tools.browser_session import BrowserSession


class _FakeMouse:
    def __init__(self):
        self.actions: list = []

    async def click(self, x, y, button="left", click_count=1):
        self.actions.append(("click", x, y, button, click_count))

    async def wheel(self, dx, dy):
        self.actions.append(("wheel", dx, dy))

    async def move(self, x, y, steps=1):
        self.actions.append(("move", x, y))

    async def down(self):
        self.actions.append(("down",))

    async def up(self):
        self.actions.append(("up",))


class _FakeKeyboard:
    def __init__(self):
        self.typed: list = []

    async def type(self, text, delay=0):
        self.typed.append((text, delay))


class _FakeLocatorTarget:
    def __init__(self, snapshot="- button 'Enviar'"):
        self._snapshot = snapshot
        self.scrolled = False

    async def scroll_into_view_if_needed(self, timeout=None):
        self.scrolled = True

    async def aria_snapshot(self):
        return self._snapshot


class _FakeLocator:
    def __init__(self, target):
        self.first = target


class _FakePage:
    url = "https://example.com"

    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.locator_target = _FakeLocatorTarget()
        self.hovered = None

    def locator(self, selector):
        return _FakeLocator(self.locator_target)

    async def hover(self, selector, timeout=None):
        self.hovered = selector


@pytest.fixture
def fake_page(monkeypatch):
    page = _FakePage()

    async def fake_require_page():
        return page, None

    monkeypatch.setattr(bt, "_require_page", fake_require_page)
    return page


class TestComputerUse:
    @pytest.mark.asyncio
    async def test_click_at(self, fake_page):
        r = await bt._browser_click_at(100, 250, button="left", click_count=2)
        assert r["clicked_at"] == [100, 250]
        assert ("click", 100, 250, "left", 2) in fake_page.mouse.actions

    @pytest.mark.asyncio
    async def test_click_at_invalid_button(self, fake_page):
        r = await bt._browser_click_at(1, 1, button="quadruple")
        assert "error" in r

    @pytest.mark.asyncio
    async def test_scroll_directions(self, fake_page):
        await bt._browser_scroll("down", 500)
        await bt._browser_scroll("up", 300)
        assert ("wheel", 0, 500) in fake_page.mouse.actions
        assert ("wheel", 0, -300) in fake_page.mouse.actions
        r = await bt._browser_scroll("diagonal")
        assert "error" in r

    @pytest.mark.asyncio
    async def test_scroll_to_selector(self, fake_page):
        r = await bt._browser_scroll(selector="#rodape")
        assert r["scrolled_to"] == "#rodape"
        assert fake_page.locator_target.scrolled is True

    @pytest.mark.asyncio
    async def test_type_and_length_cap(self, fake_page):
        r = await bt._browser_type("olá mundo", delay_ms=10)
        assert r["typed_chars"] == 9
        assert fake_page.keyboard.typed == [("olá mundo", 10)]
        r2 = await bt._browser_type("x" * 5001)
        assert "error" in r2

    @pytest.mark.asyncio
    async def test_hover_selector_and_coords(self, fake_page):
        r = await bt._browser_hover(selector=".menu")
        assert r["hovered"] == ".menu"
        r2 = await bt._browser_hover(x=10, y=20)
        assert r2["hovered_at"] == [10, 20]
        r3 = await bt._browser_hover()
        assert "error" in r3

    @pytest.mark.asyncio
    async def test_drag(self, fake_page):
        r = await bt._browser_drag(10, 10, 200, 300)
        assert r["dragged"]["to"] == [200, 300]
        kinds = [a[0] for a in fake_page.mouse.actions]
        assert kinds == ["move", "down", "move", "up"]

    @pytest.mark.asyncio
    async def test_aria_snapshot(self, fake_page):
        r = await bt._browser_aria_snapshot("form")
        assert "button 'Enviar'" in r["snapshot"]
        assert r["truncated"] is False


class TestEtapa5:
    @pytest.mark.asyncio
    async def test_upload_validates_workspace(self, fake_page, tmp_path, monkeypatch):
        import alpha.tools.workspace as ws_mod

        monkeypatch.setattr(ws_mod, "AGENT_WORKSPACE", tmp_path)

        calls = []

        async def set_input_files(selector, path, timeout=None):
            calls.append((selector, path))

        fake_page.set_input_files = set_input_files

        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF")
        r = await bt._browser_upload("input[type=file]", str(f))
        assert r["uploaded"] == str(f)
        assert calls

        r2 = await bt._browser_upload("input", "/etc/passwd")
        assert "error" in r2 and not len(calls) > 1

    @pytest.mark.asyncio
    async def test_wait_for_navigation(self, fake_page):
        waits = []

        async def wait_for_load_state(state, timeout=None):
            waits.append(("state", state))

        async def wait_for_url(rx, wait_until=None, timeout=None):
            waits.append(("url", rx.pattern, wait_until))

        fake_page.wait_for_load_state = wait_for_load_state
        fake_page.wait_for_url = wait_for_url

        r = await bt._browser_wait_for_navigation()
        assert r["state"] == "load" and ("state", "load") in waits

        r2 = await bt._browser_wait_for_navigation(url_pattern=r"dashboard", state="networkidle")
        assert ("url", "dashboard", "networkidle") in waits
        assert "error" not in r2

        r3 = await bt._browser_wait_for_navigation(url_pattern="[")
        assert "error" in r3
        r4 = await bt._browser_wait_for_navigation(state="meio-carregado")
        assert "error" in r4


class TestObservability:
    @pytest.fixture
    def session(self, monkeypatch):
        s = BrowserSession()
        monkeypatch.setattr(bt, "PLAYWRIGHT_AVAILABLE", True)

        async def fake_require_session():
            return s, None

        monkeypatch.setattr(bt, "_require_session", fake_require_session)
        return s

    @pytest.mark.asyncio
    async def test_read_console_filters(self, session):
        session.console_logs.extend([
            {"ts": 1.0, "level": "log", "text": "boot ok", "page_url": "u"},
            {"ts": 2.0, "level": "error", "text": "TypeError: x is null", "page_url": "u"},
            {"ts": 3.0, "level": "error", "text": "fetch failed 500", "page_url": "u"},
        ])
        r = await bt._browser_read_console(level="error")
        assert r["matched"] == 2
        r2 = await bt._browser_read_console(pattern="TypeError")
        assert r2["matched"] == 1
        assert "x is null" in r2["entries"][0]["text"]
        r3 = await bt._browser_read_console(pattern="[")
        assert "error" in r3

    @pytest.mark.asyncio
    async def test_read_network_filters(self, session):
        session.network_events.extend([
            {"ts": 1.0, "method": "GET", "url": "https://a.com/app.js", "status": 200,
             "content_type": "text/javascript"},
            {"ts": 2.0, "method": "POST", "url": "https://a.com/api/login", "status": 500,
             "content_type": "application/json"},
            {"ts": 3.0, "method": "GET", "url": "https://cdn.x/img.png", "status": None,
             "failure": "net::ERR_TIMED_OUT"},
        ])
        r = await bt._browser_read_network(status=500)
        assert r["matched"] == 1
        assert r["entries"][0]["url"].endswith("/api/login")
        r2 = await bt._browser_read_network(url_pattern=r"cdn\.")
        assert r2["entries"][0]["failure"] == "net::ERR_TIMED_OUT"

    def test_wire_observability_feeds_buffers(self):
        session = BrowserSession()
        handlers: dict = {}

        class _Page:
            url = "https://site.com"

            def on(self, event, cb):
                handlers[event] = cb

        session._wire_observability(_Page())

        class _Msg:
            type = "error"
            text = "ReferenceError: y"

        handlers["console"](_Msg())
        assert session.console_logs[-1]["level"] == "error"

        class _Req:
            method = "GET"
            url = "https://site.com/x"
            failure = "net::ERR_ABORTED"

        class _Resp:
            url = "https://site.com/x"
            status = 404
            headers = {"content-type": "text/html"}
            request = _Req()

        handlers["response"](_Resp())
        assert session.network_events[-1]["status"] == 404
        handlers["requestfailed"](_Req())
        assert session.network_events[-1]["failure"] == "net::ERR_ABORTED"

    def test_buffers_capped(self):
        session = BrowserSession()
        for i in range(600):
            session.console_logs.append({"ts": i, "level": "log", "text": str(i)})
        assert len(session.console_logs) == 500
        assert session.console_logs[0]["text"] == "100"
