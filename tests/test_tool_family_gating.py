"""Tier 1: gate whole tool families (browser, apify) out of the advertised
tool list when their dependency/config is absent, without removing them from
the registry (dispatch and TOOL_REGISTRY stay intact)."""


from alpha.tools import TOOL_REGISTRY, get_openai_tools, get_tool, load_all_tools

load_all_tools()


def _advertised():
    return {t["function"]["name"] for t in get_openai_tools()}


def _browser_names(names):
    return {n for n in names if n.startswith("browser_")}


def _apify_names(names):
    return {n for n in names if n.startswith("apify_")}


class TestApifyGating:
    def test_apify_dropped_without_token(self, monkeypatch):
        monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
        monkeypatch.delenv("ALPHA_APIFY_TOOLS", raising=False)
        assert not _apify_names(_advertised())

    def test_apify_advertised_with_token(self, monkeypatch):
        monkeypatch.setenv("APIFY_API_TOKEN", "tok-123")
        monkeypatch.delenv("ALPHA_APIFY_TOOLS", raising=False)
        assert _apify_names(_advertised())

    def test_apify_force_on_without_token(self, monkeypatch):
        monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
        monkeypatch.setenv("ALPHA_APIFY_TOOLS", "on")
        assert _apify_names(_advertised())

    def test_apify_force_off_with_token(self, monkeypatch):
        monkeypatch.setenv("APIFY_API_TOKEN", "tok-123")
        monkeypatch.setenv("ALPHA_APIFY_TOOLS", "off")
        assert not _apify_names(_advertised())

    def test_registry_and_dispatch_intact_when_gated(self, monkeypatch):
        # Gating only hides from the model — the tool still exists and dispatches.
        monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
        monkeypatch.setenv("ALPHA_APIFY_TOOLS", "off")
        assert "apify_run_actor" in TOOL_REGISTRY
        assert get_tool("apify_run_actor") is not None


class TestBrowserGating:
    def test_browser_force_off_drops_family(self, monkeypatch):
        monkeypatch.setenv("ALPHA_BROWSER_TOOLS", "off")
        assert not _browser_names(_advertised())
        # Registry still has them.
        assert "browser_open" in TOOL_REGISTRY

    def test_browser_force_on(self, monkeypatch):
        monkeypatch.setenv("ALPHA_BROWSER_TOOLS", "on")
        assert _browser_names(_advertised())


class TestTristateFlag:
    def test_values(self, monkeypatch):
        from alpha.config import _tristate_flag
        for v in ("1", "true", "yes", "on", "ON"):
            monkeypatch.setenv("X", v)
            assert _tristate_flag("X", auto_default=False) is True
        for v in ("0", "false", "no", "off"):
            monkeypatch.setenv("X", v)
            assert _tristate_flag("X", auto_default=True) is False
        monkeypatch.delenv("X", raising=False)
        assert _tristate_flag("X", auto_default=True) is True
        assert _tristate_flag("X", auto_default=False) is False
