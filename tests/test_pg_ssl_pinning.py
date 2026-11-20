"""Regression tests for PostgreSQL SSL/SSRF pinning.

Cobre AUDIT_V1.2 #015 (DNS rebinding via asyncpg re-resolve) e DEEP_SECURITY
V3.0 #D114 (sslmode default era `prefer` — aceita plaintext).

Estrategia: mockamos `asyncpg.create_pool` para inspecionar como o
`_get_pg_pool` constroi os argumentos, sem precisar de PG real.
"""

from __future__ import annotations

import ssl
from unittest import mock

import pytest

from alpha.tools.database_tools import (
    _LOOPBACK_HOSTS,
    _build_pg_ssl_context,
)


class TestBuildPgSslContext:
    """`_build_pg_ssl_context` decide TLS vs plaintext por hostname."""

    @pytest.mark.parametrize("conn", [
        "postgresql://localhost:5432/db",
        "postgresql://127.0.0.1/db",
        "postgresql://[::1]/db",
        "postgresql://localhost/db?sslmode=require",  # loopback ainda sem TLS
    ])
    def test_loopback_no_ssl(self, conn):
        assert _build_pg_ssl_context(conn) is False

    def test_remote_returns_default_ssl_context(self):
        ctx = _build_pg_ssl_context("postgresql://my.db.aws.com:5432/db")
        assert isinstance(ctx, ssl.SSLContext)

    def test_remote_with_sslmode_disable_returns_false_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            ctx = _build_pg_ssl_context(
                "postgresql://my.db.aws.com/db?sslmode=disable"
            )
        assert ctx is False
        assert any(
            "sslmode=disable" in rec.message
            for rec in caplog.records
        )


class TestPgPoolPinning:
    """`_get_pg_pool` pina IP via `host=hostname, hostaddr=ip` para hosts
    remotos, e usa connection string crua para loopback."""

    def setup_method(self):
        # Limpa cache de pools entre testes — diferente conn → diferente pool.
        import alpha.tools.database_tools as dbt
        dbt._pg_pools.clear()

    def _inject_fake_asyncpg(self, fake_create):
        """asyncpg nao esta instalado no venv de dev — injetamos um stub
        no sys.modules para satisfazer `import asyncpg` em `_get_pg_pool`.
        """
        import sys
        import types

        fake_module = types.ModuleType("asyncpg")
        fake_module.create_pool = fake_create
        sys.modules["asyncpg"] = fake_module
        return fake_module

    @pytest.mark.asyncio
    async def test_remote_pool_uses_hostaddr_pinning(self):
        import alpha.tools.database_tools as dbt

        fake_pool = mock.AsyncMock()
        fake_create = mock.AsyncMock(return_value=fake_pool)
        self._inject_fake_asyncpg(fake_create)
        fake_resolve = mock.AsyncMock(return_value="203.0.113.42")

        with mock.patch.object(dbt, "_resolve_and_validate", fake_resolve):
            await dbt._get_pg_pool(
                "postgresql://user:pw@my.db.aws.com:5433/mydb"
            )

        fake_resolve.assert_awaited_once_with("my.db.aws.com")
        kwargs = fake_create.call_args.kwargs
        assert kwargs["host"] == "my.db.aws.com"
        assert kwargs["hostaddr"] == "203.0.113.42"
        assert kwargs["port"] == 5433
        assert kwargs["user"] == "user"
        assert kwargs["password"] == "pw"
        assert kwargs["database"] == "mydb"
        assert isinstance(kwargs["ssl"], ssl.SSLContext)

    @pytest.mark.asyncio
    async def test_loopback_pool_uses_raw_dsn(self):
        import alpha.tools.database_tools as dbt

        fake_pool = mock.AsyncMock()
        fake_create = mock.AsyncMock(return_value=fake_pool)
        self._inject_fake_asyncpg(fake_create)
        fake_resolve = mock.AsyncMock()

        with mock.patch.object(dbt, "_resolve_and_validate", fake_resolve):
            await dbt._get_pg_pool("postgresql://localhost:5432/db")

        fake_resolve.assert_not_awaited()
        args, kwargs = fake_create.call_args
        assert args == ("postgresql://localhost:5432/db",)
        assert kwargs["ssl"] is False

    @pytest.mark.asyncio
    async def test_remote_with_sslmode_disable_still_pins_ip(self):
        """Mesmo opt-out de SSL, IP-pinning continua ativo (defesa em camadas)."""
        import alpha.tools.database_tools as dbt

        fake_pool = mock.AsyncMock()
        fake_create = mock.AsyncMock(return_value=fake_pool)
        self._inject_fake_asyncpg(fake_create)
        fake_resolve = mock.AsyncMock(return_value="198.51.100.7")

        with mock.patch.object(dbt, "_resolve_and_validate", fake_resolve):
            await dbt._get_pg_pool(
                "postgresql://my.db.aws.com/db?sslmode=disable"
            )

        kwargs = fake_create.call_args.kwargs
        assert kwargs["hostaddr"] == "198.51.100.7"
        assert kwargs["ssl"] is False

    @pytest.mark.asyncio
    async def test_resolve_validate_failure_propagates(self):
        """Se `_resolve_and_validate` raise (SSRF detectado / DNS fail),
        a excecao deve propagar — sem fallback para connection string crua."""
        import alpha.tools.database_tools as dbt

        fake_create = mock.AsyncMock()
        self._inject_fake_asyncpg(fake_create)
        fake_resolve = mock.AsyncMock(
            side_effect=ValueError("IP 10.0.0.1 is private")
        )

        with mock.patch.object(dbt, "_resolve_and_validate", fake_resolve):
            with pytest.raises(ValueError, match="private"):
                await dbt._get_pg_pool("postgresql://attacker.com/db")

        fake_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_cached_per_connection_string(self):
        """Mesma connection string → mesmo pool (cache hit). Garante que a
        re-resolucao do IP acontece UMA vez por connection key."""
        import alpha.tools.database_tools as dbt

        fake_pool = mock.AsyncMock()
        fake_create = mock.AsyncMock(return_value=fake_pool)
        self._inject_fake_asyncpg(fake_create)
        fake_resolve = mock.AsyncMock(return_value="203.0.113.99")

        conn = "postgresql://my.db.aws.com/db"
        with mock.patch.object(dbt, "_resolve_and_validate", fake_resolve):
            p1 = await dbt._get_pg_pool(conn)
            p2 = await dbt._get_pg_pool(conn)

        assert p1 is p2
        assert fake_resolve.await_count == 1
        assert fake_create.await_count == 1


class TestLoopbackConstantsCovered:
    """Garante que `_LOOPBACK_HOSTS` cobre os hosts loopback canonicos."""

    @pytest.mark.parametrize("h", ["localhost", "127.0.0.1", "::1", ""])
    def test_host_in_loopback(self, h):
        assert h in _LOOPBACK_HOSTS


class TestPgPoolLoopAware:
    """#001: asyncpg pools are bound to their creation loop. The CLI runs
    asyncio.run() per turn (new loop each turn), so a cached pool reused on a
    later turn crashes with 'attached to a different loop'. The cache must
    rebuild when the loop changes."""

    def setup_method(self):
        import alpha.tools.database_tools as dbt
        dbt._pg_pools.clear()
        dbt._pg_pools_loop = None

    def _inject_fake_asyncpg(self, fake_create):
        import sys
        import types
        m = types.ModuleType("asyncpg")
        m.create_pool = fake_create
        sys.modules["asyncpg"] = m

    def test_pool_rebuilt_on_new_loop(self):
        import asyncio
        import alpha.tools.database_tools as dbt

        calls = {"n": 0}

        async def fake_create(*a, **k):
            calls["n"] += 1
            return object()  # a distinct pool per build

        self._inject_fake_asyncpg(fake_create)
        conn = "postgresql://localhost:5432/db"  # loopback → no SSRF resolve

        async def turn():
            return await dbt._get_pg_pool(conn)

        p1 = asyncio.run(turn())  # loop A
        p2 = asyncio.run(turn())  # loop B (fresh loop) — must rebuild, not reuse
        assert calls["n"] == 2
        assert p1 is not p2

    def test_pool_cached_within_same_loop(self):
        import asyncio
        import alpha.tools.database_tools as dbt

        calls = {"n": 0}

        async def fake_create(*a, **k):
            calls["n"] += 1
            return object()

        self._inject_fake_asyncpg(fake_create)
        conn = "postgresql://localhost:5432/db"

        async def turn():
            a = await dbt._get_pg_pool(conn)
            b = await dbt._get_pg_pool(conn)
            assert a is b  # cached within the loop
            return a

        asyncio.run(turn())
        assert calls["n"] == 1
