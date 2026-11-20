"""Browser Etapa 2 — loop dedicado (sessão sobrevive entre turnos) e perfil
persistente (logins salvos entre sessões do Alpha).

Sem Playwright real: o loop/dispatcher são infra pura, e o open() persistente
é exercitado com um Playwright falso injetado nos globals do módulo.
"""

import asyncio

import pytest

import alpha.tools.browser_session as bs
from alpha.tools.browser_session import (
    BrowserSession,
    _BrowserLoop,
    _profile_dir,
    run_on_browser_loop,
)


@pytest.fixture(autouse=True)
def _clean():
    yield
    BrowserSession._instance = None
    _BrowserLoop.stop()


class TestBrowserLoop:
    def test_same_loop_across_asyncio_run_calls(self):
        """O ponto da Etapa 2: dois 'turnos' (asyncio.run distintos) veem o
        MESMO loop de browser — antes, cada turno matava a sessão."""

        async def captura():
            fut = _BrowserLoop.submit(_current_loop())
            return await asyncio.wrap_future(fut)

        async def _current_loop():
            return id(asyncio.get_running_loop())

        loop1 = asyncio.run(captura())
        loop2 = asyncio.run(captura())
        assert loop1 == loop2

    def test_browser_loop_is_not_caller_loop(self):
        async def check():
            caller = id(asyncio.get_running_loop())
            fut = _BrowserLoop.submit(_here())
            browser = await asyncio.wrap_future(fut)
            return caller, browser

        async def _here():
            return id(asyncio.get_running_loop())

        caller, browser = asyncio.run(check())
        assert caller != browser

    def test_stop_and_restart_gives_fresh_loop(self):
        loop_a = _BrowserLoop.loop()
        _BrowserLoop.stop()
        assert not _BrowserLoop.alive()
        loop_b = _BrowserLoop.loop()
        assert loop_b is not loop_a
        assert _BrowserLoop.alive()


class TestRunOnBrowserLoop:
    def test_wrapped_executor_runs_on_browser_loop(self):
        @run_on_browser_loop
        async def executor():
            return id(asyncio.get_running_loop())

        result = asyncio.run(executor())
        assert result == id(_BrowserLoop._loop)

    def test_nested_call_executes_inline(self):
        """Tool composta chamando outra browser tool JÁ no loop do browser
        não pode re-despachar (deadlock) — executa inline."""

        @run_on_browser_loop
        async def inner():
            return "ok"

        @run_on_browser_loop
        async def outer():
            return await inner()

        assert asyncio.run(outer()) == "ok"

    def test_args_and_errors_propagate(self):
        @run_on_browser_loop
        async def soma(a, b=1):
            if a < 0:
                raise ValueError("negativo")
            return a + b

        assert asyncio.run(soma(2, b=3)) == 5
        with pytest.raises(ValueError):
            asyncio.run(soma(-1))


class TestProfileDir:
    def test_env_override_and_permissions(self, tmp_path, monkeypatch):
        target = tmp_path / "meu-perfil"
        monkeypatch.setenv("ALPHA_BROWSER_PROFILE_DIR", str(target))
        d = _profile_dir()
        assert d == target.resolve()
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700


# ─── Fake Playwright para exercitar open() sem Chromium ───


class _FakePage:
    url = "about:blank"

    def on(self, *a, **k):
        pass


class _FakeContext:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else []
        self.closed = False

    def on(self, *a, **k):
        pass

    def remove_listener(self, *a, **k):
        pass

    async def new_page(self):
        p = _FakePage()
        self.pages.append(p)
        return p

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def new_context(self, **kw):
        return _FakeContext()

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self):
        self.persistent_dirs: list[str] = []

    async def launch(self, headless=True):
        return _FakeBrowser()

    async def launch_persistent_context(self, user_data_dir, **kw):
        self.persistent_dirs.append(user_data_dir)
        return _FakeContext([_FakePage()])


class _FakePW:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_playwright(monkeypatch, tmp_path):
    pw = _FakePW()

    class _FakeAP:
        async def start(self):
            return pw

    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(bs, "_ensure_playwright_loaded", lambda: True)
    monkeypatch.setattr(bs, "async_playwright", lambda: _FakeAP())
    monkeypatch.setenv("ALPHA_BROWSER_PROFILE_DIR", str(tmp_path / "perfil"))
    BrowserSession._instance = None
    return pw


class TestPersistentProfile:
    @pytest.mark.asyncio
    async def test_open_with_profile_uses_persistent_context(self, fake_playwright):
        session = await BrowserSession.get()
        await session.open(headless=True, profile=True)
        assert session.persistent is True
        assert session.browser is None
        assert session.profile_dir and "perfil" in session.profile_dir
        assert fake_playwright.chromium.persistent_dirs == [session.profile_dir]
        assert session.is_open() is True

    @pytest.mark.asyncio
    async def test_open_without_profile_unchanged(self, fake_playwright):
        session = await BrowserSession.get()
        await session.open(headless=True)
        assert session.persistent is False
        assert session.browser is not None
        assert session.is_open() is True

    @pytest.mark.asyncio
    async def test_mode_switch_reopens(self, fake_playwright):
        session = await BrowserSession.get()
        await session.open(headless=True)
        first_browser = session.browser
        assert first_browser is not None

        await session.open(headless=True, profile=True)
        assert session.persistent is True
        assert session.browser is None
        assert first_browser.closed is True  # sessão anterior fechada de verdade

    @pytest.mark.asyncio
    async def test_close_persistent_saves_context(self, fake_playwright):
        session = await BrowserSession.get()
        await session.open(profile=True)
        ctx = session.context
        await session.close()
        assert ctx.closed is True
        assert BrowserSession._instance is None

    @pytest.mark.asyncio
    async def test_same_mode_reuses_session(self, fake_playwright):
        session = await BrowserSession.get()
        await session.open(profile=True)
        ctx = session.context
        await session.open(profile=True)  # no-op
        assert session.context is ctx


class TestBrowserOpenTool:
    def test_profile_default_env(self, monkeypatch):
        from alpha.tools.browser_tools import _profile_default

        monkeypatch.delenv("ALPHA_BROWSER_PROFILE", raising=False)
        assert _profile_default() is False
        monkeypatch.setenv("ALPHA_BROWSER_PROFILE", "1")
        assert _profile_default() is True

    @pytest.mark.asyncio
    async def test_tool_reports_profile_mode(self, fake_playwright, monkeypatch):
        import alpha.tools.browser_tools as bt

        monkeypatch.setattr(bt, "PLAYWRIGHT_AVAILABLE", True)
        r = await bt._browser_open(headless=True, profile=True)
        assert r["status"] == "opened"
        assert r["profile"] is True
        assert "profile_dir" in r

        r2 = await bt._browser_open(headless=True, profile=True)
        assert r2["status"] == "already_open"

        r3 = await bt._browser_open(headless=True, profile=False)
        assert r3["status"] == "reopened"
        assert r3["profile"] is False
