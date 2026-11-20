"""More quick wins from DEEP_MAINTAINABILITY + DEEP_PERFORMANCE.

Cobre:
- #DM007 — .env.example documenta novas variaveis
- #086 — wizard `_PROVIDERS` derivado de config
- #095 — `_PROJECT_ROOT` compartilhado
- #096 — index comment em tools/__init__.py
- #D010 — display truncation constants centralizadas
- #D022 — extract_page_content TTL cache
"""

from pathlib import Path

import pytest


# ─── #DM007 — .env.example ─────────────────────────────────────────


class TestEnvExampleComplete:
    def _read_env_example(self) -> str:
        return (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")

    def test_documents_browser_allowlist(self):
        text = self._read_env_example()
        assert "ALPHA_BROWSER_ALLOWLIST" in text
        assert "ALPHA_BROWSER_REQUIRE_ALLOWLIST" in text

    def test_documents_apify_token(self):
        text = self._read_env_example()
        assert "APIFY_API_TOKEN" in text

    def test_documents_no_color(self):
        text = self._read_env_example()
        assert "NO_COLOR" in text

    def test_documents_alpha_agent(self):
        text = self._read_env_example()
        assert "ALPHA_AGENT" in text


# ─── #086 — wizard providers from config ───────────────────────────


class TestWizardProvidersFromConfig:
    # NB: the former `test_wizard_imports_config_providers` source-grep was
    # removed — `test_wizard_provider_count_matches_config` below proves the
    # wizard's `_PROVIDERS` is derived from config (identical id-set), which an
    # independently hand-maintained list could not satisfy.

    def test_wizard_provider_count_matches_config(self):
        from alpha.config import _PROVIDERS as cfg
        from alpha.wizard.steps import _PROVIDERS as wiz

        assert len(wiz) == len(cfg)
        cfg_ids = set(cfg.keys())
        wiz_ids = {p["id"] for p in wiz}
        assert cfg_ids == wiz_ids


# ─── #095 — _PROJECT_ROOT shared ───────────────────────────────────


class TestProjectRootShared:
    def test_wizard_env_shares_config_project_root(self):
        """#095: env reuses config.PROJECT_ROOT rather than recomputing its
        own. Identity proof — the bound object IS the one config exports, which
        a local `Path(__file__)...` definition could never satisfy."""
        from alpha import config
        from alpha.wizard import env

        assert env.PROJECT_ROOT is config.PROJECT_ROOT

    def test_agents_registry_shares_config_project_root(self):
        """#095: agents.registry reuses config.PROJECT_ROOT (same object)."""
        from alpha import config
        from alpha.agents import registry

        assert registry.PROJECT_ROOT is config.PROJECT_ROOT

    # NB: the former `test_wizard_steps_imports_from_config` source-grep was
    # removed — it only passed because the string "_PROJECT_ROOT" appears in a
    # docstring (steps no longer binds it). The real config coupling for steps
    # is its `_PROVIDERS`, which TestWizardProvidersFromConfig covers.


# ─── #096 — tools index comment ────────────────────────────────────


class TestToolsIndexComment:
    def test_module_docstring_lists_tools(self):
        from alpha import tools

        doc = tools.__doc__ or ""
        # Documenta onde cada tool vive (helps grep / IDE jump-to-def
        # readers find the right module).
        assert "file_tools.py" in doc
        assert "shell_tools.py" in doc
        assert "delegate_tools.py" in doc


# ─── #D010 — display constants ─────────────────────────────────────


class TestDisplayConstants:
    def test_constants_exported(self):
        from alpha import display

        assert isinstance(getattr(display, "DISPLAY_LINE_TRUNCATE", None), int) and display.DISPLAY_LINE_TRUNCATE > 0
        assert isinstance(getattr(display, "DISPLAY_PREVIEW_TRUNCATE", None), int) and display.DISPLAY_PREVIEW_TRUNCATE > 0
        assert isinstance(getattr(display, "DISPLAY_PROMPT_VALUE_TRUNCATE", None), int) and display.DISPLAY_PROMPT_VALUE_TRUNCATE > 0
        assert isinstance(getattr(display, "DISPLAY_MAX_LINES", None), int) and display.DISPLAY_MAX_LINES > 0

    def test_render_path_honors_preview_constant(self):
        """#D010: the centralized constants are actually wired into the render
        path, not shadowed by inline magic numbers. Behavioral proof: the
        tool-args preview truncates a long value to exactly
        DISPLAY_PREVIEW_TRUNCATE chars (ending in the ellipsis marker). If a
        renderer reverted to a literal `[:200]`/`[:120]`, this length would no
        longer track the constant and the test would fail."""
        from alpha import display
        from alpha.display.renderers.tools import _tool_args_preview

        limit = display.DISPLAY_PREVIEW_TRUNCATE
        out = _tool_args_preview({"command": "x" * (limit * 4)})
        assert len(out) == limit
        assert out.endswith("…")

    def test_truncate_helper_respects_arbitrary_limit(self):
        """The shared `_truncate` helper drives every render truncation; it
        honours the limit it is passed (proving truncation is parameterized by
        the constants rather than hardcoded)."""
        from alpha.display.theme import _truncate

        assert _truncate("short", 50) == "short"
        out = _truncate("y" * 100, 30)
        assert len(out) == 30
        assert out.endswith("…")


# ─── #D022 — extract cache ─────────────────────────────────────────


class TestExtractCache:
    def test_cache_attrs_exist(self):
        from alpha import web_search

        assert hasattr(web_search, "_EXTRACT_CACHE")
        assert hasattr(web_search, "_EXTRACT_CACHE_TTL")
        assert hasattr(web_search, "_EXTRACT_CACHE_MAX")

    @pytest.mark.asyncio
    async def test_cache_returns_same_result_within_ttl(self, monkeypatch):
        from alpha import web_search

        web_search._EXTRACT_CACHE.clear()

        call_count = {"n": 0}

        async def fake_fetch_raw(url, timeout, max_bytes):
            call_count["n"] += 1
            return b"<html><body>Hello world</body></html>", {}, 200

        monkeypatch.setattr(web_search, "_fetch_raw", fake_fetch_raw)

        a = await web_search.extract_page_content("https://example.com")
        b = await web_search.extract_page_content("https://example.com")

        assert a == b
        # Segunda chamada NAO deve ter incrementado (cache hit)
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_cache_evicts_oldest(self, monkeypatch):
        from alpha import web_search

        web_search._EXTRACT_CACHE.clear()

        async def fake_fetch_raw(url, timeout, max_bytes):
            return b"<p>fake content</p>", {}, 200

        monkeypatch.setattr(web_search, "_fetch_raw", fake_fetch_raw)
        monkeypatch.setattr(web_search, "_EXTRACT_CACHE_MAX", 3)

        for i in range(5):
            await web_search.extract_page_content(f"https://example.com/{i}")

        assert len(web_search._EXTRACT_CACHE) <= 3
