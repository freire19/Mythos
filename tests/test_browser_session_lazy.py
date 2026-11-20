"""Regressao para AUDIT_V1.8 #R202 — Playwright lazy import sem try/except.

Pre-fix: `_ensure_playwright_loaded()` fazia `from playwright.async_api import ...`
diretamente. Se Playwright top-level esta no PATH mas com sub-dep quebrada
(corrupcao, ABI mismatch, wheel parcial), o ImportError propagava cru.

Pos-fix: ImportError no real import e capturado e a funcao retorna False —
caller exibe o mesmo RuntimeError amigavel que ja existia pro caso "not installed".
"""

from __future__ import annotations

import sys


from alpha.tools import browser_session as bs


def test_ensure_playwright_loaded_returns_false_on_subdep_import_error(monkeypatch):
    """Simula install corrupto: top-level presente, sub-import dispara ImportError."""
    # Reset state — `async_playwright` precisa estar None para o try/except path ser exercitado
    monkeypatch.setattr(bs, "async_playwright", None)
    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", True)

    # Forca o `from playwright.async_api import ...` a falhar como se sub-dep
    # estivesse corrompida. Mock do submodulo no sys.modules dispara ImportError
    # no proximo import.
    fake_module_name = "playwright.async_api"

    class _BrokenModule:
        def __getattr__(self, item):
            raise ImportError(
                f"cannot import name '{item}' from 'playwright.async_api' "
                f"(simulated: greenlet missing or ABI mismatch)"
            )

    # Patch sys.modules para fazer "from playwright.async_api import X" falhar
    original_module = sys.modules.get(fake_module_name)
    sys.modules[fake_module_name] = _BrokenModule()  # type: ignore[assignment]
    try:
        # Funcao deve capturar ImportError e retornar False — sem traceback cru
        result = bs._ensure_playwright_loaded()
        assert result is False
        # Symbols devem permanecer None — nada foi rebound
        assert bs.async_playwright is None
        assert bs.Browser is None
        assert bs.Page is None
    finally:
        if original_module is not None:
            sys.modules[fake_module_name] = original_module
        else:
            sys.modules.pop(fake_module_name, None)


def test_ensure_playwright_loaded_returns_false_when_not_available(monkeypatch):
    """Caso ja-suportado pre-fix: PLAYWRIGHT_AVAILABLE=False → return False."""
    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", False)
    assert bs._ensure_playwright_loaded() is False


def test_ensure_playwright_loaded_idempotent(monkeypatch):
    """Se async_playwright ja esta carregado, retorno e True sem re-importar."""
    sentinel = object()
    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(bs, "async_playwright", sentinel)
    assert bs._ensure_playwright_loaded() is True
    # async_playwright preservado — nao re-importou
    assert bs.async_playwright is sentinel
