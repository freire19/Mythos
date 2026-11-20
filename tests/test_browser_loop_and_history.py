"""#002 + #D137: BrowserSession loop-awareness and history-nav re-validation.

Neither test needs a real Playwright runtime — they exercise the pure Python
logic (loop tracking, URL re-validation) with lightweight fakes.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from alpha.tools import browser_session as bs
from alpha.tools import browser_tools as bt


# ── #002: is_open() is loop-aware ──

def test_is_open_false_when_loop_changed():
    sess = bs.BrowserSession()
    fake_browser = mock.Mock()
    fake_browser.is_connected.return_value = True
    sess.browser = fake_browser
    sess._loop = object()  # a different "loop" than whatever runs the test

    async def check():
        # current running loop != sess._loop → stale → not open
        return sess.is_open()

    assert asyncio.run(check()) is False


def test_is_open_true_on_same_loop():
    sess = bs.BrowserSession()
    fake_browser = mock.Mock()
    fake_browser.is_connected.return_value = True
    sess.browser = fake_browser

    async def check():
        sess._loop = asyncio.get_running_loop()  # bind to the running loop
        return sess.is_open()

    assert asyncio.run(check()) is True


def test_discard_stale_session_clears_refs():
    sess = bs.BrowserSession()
    sess.browser = mock.Mock()
    sess.context = mock.Mock()
    sess.playwright = mock.Mock()
    sess.pages = [mock.Mock()]
    sess._loop = object()
    sess._discard_stale_session()
    assert sess.browser is None and sess.context is None
    assert sess.playwright is None and sess.pages == []
    assert sess._loop is None


# ── #D137: history nav re-validates the landed URL ──

class _FakePage:
    def __init__(self, landed_url: str):
        self.url = landed_url
        self.goto_calls: list = []

    async def go_back(self):
        return None

    async def go_forward(self):
        return None

    async def goto(self, url, **kw):
        self.goto_calls.append(url)
        self.url = url

    async def title(self):
        return "t"


@pytest.mark.asyncio
async def test_history_nav_blocks_internal_url(monkeypatch):
    page = _FakePage("file:///etc/passwd")
    out = await bt._history_nav(page, page.go_back)
    assert out.get("blocked") is True
    assert "about:blank" in page.goto_calls  # rescued away


@pytest.mark.asyncio
async def test_history_nav_blocks_disallowed_domain(monkeypatch):
    # Force the allowlist to reject everything.
    monkeypatch.setattr(bt, "_domain_allowed", lambda url: "domain not allowed")
    page = _FakePage("http://evil.example.com/")
    out = await bt._history_nav(page, page.go_forward)
    assert out.get("blocked") is True
    assert "about:blank" in page.goto_calls


@pytest.mark.asyncio
async def test_history_nav_allows_valid_url(monkeypatch):
    monkeypatch.setattr(bt, "_domain_allowed", lambda url: None)
    page = _FakePage("https://example.com/page")
    out = await bt._history_nav(page, page.go_back)
    assert out.get("blocked") is None
    assert out["url"] == "https://example.com/page"
    assert page.goto_calls == []  # no rescue needed


@pytest.mark.asyncio
async def test_history_nav_allows_about_blank(monkeypatch):
    page = _FakePage("about:blank")
    out = await bt._history_nav(page, page.go_back)
    assert out.get("blocked") is None
    assert page.goto_calls == []
