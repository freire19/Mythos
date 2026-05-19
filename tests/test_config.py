"""Tests for configuration system."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from alpha import config
from alpha.config import FEATURES, get_available_providers, get_provider_config, load_system_prompt


class TestConfig:
    def test_features_delegate_enabled(self):
        assert FEATURES["delegate_tool_enabled"] is True
        assert FEATURES["multi_agent_enabled"] is True

    def test_subagent_iterations_limit(self):
        assert FEATURES["subagent_max_iterations"] == 15
        assert FEATURES["max_parallel_agents"] == 3

    def test_system_prompt_loads(self):
        prompt = load_system_prompt()
        assert "ALPHA" in prompt
        assert len(prompt) > 100

    def test_available_providers(self):
        providers = get_available_providers()
        names = [p["id"] for p in providers]
        assert "deepseek" in names
        assert "openai" in names
        assert "grok" in names
        assert "ollama" in names

    def test_ollama_always_available(self):
        providers = get_available_providers()
        ollama = next(p for p in providers if p["id"] == "ollama")
        assert ollama["available"] is True


class TestProviderVisionFlag:
    """get_provider_config must expose supports_vision per provider."""

    def test_openai_supports_vision(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        cfg = get_provider_config("openai")
        assert cfg["supports_vision"] is True

    def test_anthropic_supports_vision(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        cfg = get_provider_config("anthropic")
        assert cfg["supports_vision"] is True

    def test_deepseek_does_not_support_vision(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        cfg = get_provider_config("deepseek")
        assert cfg["supports_vision"] is False

    def test_grok_does_not_support_vision(self, monkeypatch):
        monkeypatch.setenv("GROK_API_KEY", "test")
        cfg = get_provider_config("grok")
        assert cfg["supports_vision"] is False

    def test_ollama_does_not_support_vision_by_default(self):
        cfg = get_provider_config("ollama")
        assert cfg["supports_vision"] is False

    def test_default_vision_format_is_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        cfg = get_provider_config("openai")
        assert cfg["vision_format"] == "openai"


class TestDotenvDiscovery:
    """§4 PyPI prep: .env must be discoverable when alpha is installed via
    pipx (no _PROJECT_ROOT/.env in site-packages). Discovery order is
    repo .env → ~/.alpha/.env → CWD .env, with later sources overriding."""

    def test_cwd_env_overrides_user_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        (home / ".alpha").mkdir(parents=True)
        cwd.mkdir()
        (home / ".alpha" / ".env").write_text("ALPHA_TEST_VAR=from_home\n")
        (cwd / ".env").write_text("ALPHA_TEST_VAR=from_cwd\n")

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)
        monkeypatch.delenv("ALPHA_TEST_VAR", raising=False)

        loaded = config._load_env_files()
        assert os.environ.get("ALPHA_TEST_VAR") == "from_cwd"
        # Both files were loaded — the override semantics are what made CWD win.
        assert any(p.name == ".env" and p.parent == cwd for p in loaded)

    def test_missing_env_is_silent(self, tmp_path, monkeypatch):
        """No .env anywhere = no crash, no error. The 12-factor path where
        all config comes from env vars must keep working."""
        home = tmp_path / "home"
        cwd = tmp_path / "cwd"
        home.mkdir()
        cwd.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)

        loaded = config._load_env_files()
        # Only thing we might find is the dev repo's own .env — anything
        # under tmp_path was empty. Confirm we didn't crash and got a list.
        assert isinstance(loaded, list)

    def test_search_paths_include_home_alpha(self, monkeypatch, tmp_path):
        """~/.alpha/.env is the recommended location for pipx users; it
        must appear in the search chain."""
        monkeypatch.setenv("HOME", str(tmp_path))
        paths = config._dotenv_search_paths()
        assert tmp_path / ".alpha" / ".env" in paths

    def test_search_paths_include_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        paths = config._dotenv_search_paths()
        assert tmp_path / ".env" in paths
