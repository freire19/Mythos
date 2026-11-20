"""Testes de regressao DEEP_RESILIENCE wave 2 — #D804 (database_tools).

#D804: `wait_for` sobre `run_in_executor` no default pool nao cancelava a
thread SQLite — workers do pool compartilhado vazavam ate starvation.
Fix: executor dedicado/bounded + `conn.interrupt()` no timeout.
"""

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from alpha.tools import database_tools as dbt


# Query que roda "para sempre" sem escrever nada (CTE recursiva infinita).
_INFINITE_QUERY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
    "SELECT count(*) FROM c"
)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbt, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.setattr(
        "alpha.tools.workspace.AGENT_WORKSPACE", tmp_path, raising=False
    )
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO t (name) VALUES ('a'), ('b')")
    conn.commit()
    conn.close()
    return db


def test_sqlite_usa_executor_dedicado():
    ex = dbt._get_sqlite_executor()
    assert isinstance(ex, ThreadPoolExecutor)
    assert ex is dbt._get_sqlite_executor()  # singleton
    assert ex._max_workers <= 4  # bounded
    assert ex._thread_name_prefix == "alpha-sqlite"


def test_query_normal_continua_funcionando(sqlite_db):
    result = asyncio.run(
        dbt._query_sqlite(str(sqlite_db), "SELECT * FROM t ORDER BY id", True)
    )
    assert "error" not in result
    assert result["row_count"] == 2
    assert result["columns"] == ["id", "name"]


def test_timeout_retorna_dict_e_interrompe_a_thread(sqlite_db, monkeypatch):
    monkeypatch.setitem(dbt.TOOL_TIMEOUTS, "database", 0.5)

    start = time.monotonic()
    result = asyncio.run(
        dbt._query_sqlite(str(sqlite_db), _INFINITE_QUERY, True)
    )
    elapsed = time.monotonic() - start

    assert result.get("timeout") is True
    assert elapsed < 5  # retornou perto do timeout, nao pendurou

    # #D804: `conn.interrupt()` deve liberar o worker de verdade — um job
    # subsequente no executor dedicado precisa rodar rapido, sem worker preso.
    ex = dbt._get_sqlite_executor()
    deadline = time.monotonic() + 5
    freed = False
    while time.monotonic() < deadline:
        fut = ex.submit(lambda: "ok")
        try:
            if fut.result(timeout=1) == "ok":
                freed = True
                break
        except TimeoutError:
            continue
    assert freed, "worker SQLite continuou preso apos o timeout"


def test_timeout_nao_usa_default_pool(sqlite_db, monkeypatch):
    monkeypatch.setitem(dbt.TOOL_TIMEOUTS, "database", 0.5)

    from asyncio.base_events import BaseEventLoop

    called: dict = {}
    real = BaseEventLoop.run_in_executor

    def spy(self, executor, func, *args):
        called["executor"] = executor
        return real(self, executor, func, *args)

    monkeypatch.setattr(BaseEventLoop, "run_in_executor", spy)
    asyncio.run(dbt._query_sqlite(str(sqlite_db), _INFINITE_QUERY, True))
    assert called["executor"] is dbt._get_sqlite_executor()
