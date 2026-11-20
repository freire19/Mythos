"""AUDIT_V2.3 #101 — heurística compartilhada `alpha.secret_env.is_secret_env`.

Os nomes listados aqui são exatamente os que a regex antiga (#D118/#014)
deixava vazar para hooks e para /proc via args de MCP stdio. Se alguém
estreitar a heurística, estes testes falham alto.
"""

from __future__ import annotations

import pytest

from alpha.secret_env import is_secret_env


class TestNamesFormerlyLeaked:
    """Gaps do #101: nomes reais que passavam pela regex antiga."""

    @pytest.mark.parametrize("name", [
        "DATABASE_URL",
        "REDIS_URL",
        "MONGODB_URI",
        "MONGO_URI",
        "AWS_ACCESS_KEY_ID",
        "STRIPE_KEY",
        "PRIVATE_KEY",
        "SIGNING_KEY",
        "ENCRYPTION_KEY",
        "SESSION_KEY",
        "GH_PAT",
        "SENTRY_DSN",
        "PG_CONNECTION_STRING",
        "DB_URL",
    ])
    def test_secret_names_detected(self, name):
        assert is_secret_env(name)

    @pytest.mark.parametrize("name", [
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "MY_SECRET",
        "DB_PASSWORD",
        "BASIC_AUTH",
    ])
    def test_original_coverage_kept(self, name):
        assert is_secret_env(name)


class TestNoGenericUrlSuperMatch:
    """Nota da review: `_url$`/`_uri$` genéricos foram vetados —
    `BASE_URL`/`SERVER_URL`/`DOCS_URL` são config legítima, não secret."""

    @pytest.mark.parametrize("name", [
        "BASE_URL",
        "SERVER_URL",
        "DOCS_URL",
        "WEBHOOK_URL",
        "PATH",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
        "HOME",
        "MY_PROJECT_DIR",
        "LANG",
    ])
    def test_benign_names_pass(self, name):
        assert not is_secret_env(name)
        assert not is_secret_env(name, "https://example.com/x")


class TestValueHeuristic:
    """Valor inequívoco de credencial pega var de nome inocente."""

    def test_embedded_userinfo_url(self):
        assert is_secret_env("MINHA_VAR", "postgres://user:s3nh4@host:5432/db")

    def test_embedded_userinfo_empty_user(self):
        assert is_secret_env("CACHE", "redis://:s3nh4@localhost:6379/0")

    def test_pem_private_key(self):
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END..."
        assert is_secret_env("WHATEVER", pem)

    def test_plain_url_without_userinfo_passes(self):
        assert not is_secret_env("ENDPOINT", "https://api.example.com:8443/v1")

    def test_none_value_ok(self):
        assert not is_secret_env("ENDPOINT", None)
