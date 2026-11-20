"""Database query tools for ALPHA agent.

Execute SQL queries on local databases (SQLite, PostgreSQL via connection string).

SECURITY: Read-only mode by default. Write operations require explicit flag.
Only local databases allowed (SSRF protection on connection strings).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import ssl as _ssl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse

from .._security_log import sanitize_for_log
from ..net_utils import (
    is_private_ip as _is_private_ip,
    resolve_and_validate as _resolve_and_validate,
)
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ..config import TOOL_TIMEOUTS
from .workspace import AGENT_WORKSPACE, assert_within_workspace  # noqa: F401 — AGENT_WORKSPACE re-exported (monkeypatched by tests)

logger = logging.getLogger(__name__)

_MAX_ROWS = 500

# #D804: executor dedicado e bounded para SQLite. `run_in_executor(None, ...)`
# usava o default pool compartilhado (DNS/search/OCR); queries travadas
# vazavam workers ate starvation global. Aqui o pior caso fica confinado a
# estes workers, e o watchdog via `conn.interrupt()` libera a thread de fato.
_sqlite_executor: ThreadPoolExecutor | None = None


def _get_sqlite_executor() -> ThreadPoolExecutor:
    global _sqlite_executor
    if _sqlite_executor is None:
        _sqlite_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="alpha-sqlite"
        )
    return _sqlite_executor

# Pool por connection string para PostgreSQL (#D021-PERF). asyncpg.connect()
# faz handshake TCP+TLS+auth (~150-500ms remoto); cada describe_table/query
# abria conexao nova. Pool reusa, drop em sessao termina.
_pg_pools: dict = {}
# #001: o loop ao qual os pools cacheados pertencem. asyncpg amarra cada pool
# (e suas conexoes) ao loop em que foi criado; o CLI roda asyncio.run() por
# turn (loop novo a cada call), entao reusar um pool cacheado num turn ≥2
# levanta `RuntimeError: ... got Future attached to a different loop`. O fix
# anterior so deixou o LOCK loop-aware; os POOLS continuavam stale. Agora
# rastreamos o loop e descartamos o cache inteiro quando ele troca.
_pg_pools_loop: object | None = None
# Lock criado lazy, atrelado ao loop ativo. Antes era criado no module-load:
# o CLI roda asyncio.run() por turn (loop novo a cada call), enquanto o
# modulo permanece em cache de imports — na 2a turn a Lock pertencia a um
# loop fechado e disparava `RuntimeError: attached to a different loop`.
# Padrao igual ao usado em alpha/llm.py:_get_shared_llm_client.
_pg_pools_lock: asyncio.Lock | None = None
_pg_pools_lock_loop: object | None = None


def _get_pg_pools_lock() -> asyncio.Lock:
    global _pg_pools_lock, _pg_pools_lock_loop
    loop = asyncio.get_running_loop()
    if _pg_pools_lock is None or _pg_pools_lock_loop is not loop:
        _pg_pools_lock = asyncio.Lock()
        _pg_pools_lock_loop = loop
    return _pg_pools_lock


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def _build_pg_ssl_context(connection: str) -> "_ssl.SSLContext | bool":
    """SSL context para asyncpg.create_pool.

    DEEP_SECURITY V3.0 #D114: asyncpg defaulta para `sslmode=prefer`
    (aceita plaintext se o servidor nao oferecer TLS). Em conexoes
    remotas isso e MITM-able. Forcamos TLS via `create_default_context()`
    quando o destino nao for loopback. Loopback (`localhost`/`127.0.0.1`)
    fica permissivo — devs locais raramente tem TLS no PG dev.
    Opt-out via `sslmode=disable` na connection string (loga warning).
    """
    parsed = urlparse(connection)
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return False  # plaintext OK em loopback
    if "sslmode=disable" in connection.lower():
        logger.warning(
            "PostgreSQL connection com sslmode=disable em host remoto (%s) — "
            "MITM possivel. Use sslmode=require em producao.", host,
        )
        return False
    return _ssl.create_default_context()


async def _get_pg_pool(connection: str):
    """Lazy-init pool por connection string.

    AUDIT_V1.2 #015 (DNS rebinding): a versao antiga passava `connection` cru
    para asyncpg, que re-resolvia o hostname com DNS — janela onde atacante
    com TTL=0 podia retornar IP publico no primeiro check (`_is_private_ip`)
    e IP privado (e.g. `169.254.169.254` cloud metadata) no connect real.
    Agora resolvemos hostname uma vez via `_resolve_and_validate` e passamos
    `host=hostname` (cert SAN/SNI matcham) + `hostaddr=ip` (asyncpg conecta
    no IP, sem nova resolucao). Cobre tambem #D114 com `_build_pg_ssl_context`.
    """
    global _pg_pools_loop
    loop = asyncio.get_running_loop()
    # Fast path: pool exists AND was built on the CURRENT loop.
    pool = _pg_pools.get(connection)
    if pool is not None and _pg_pools_loop is loop:
        return pool
    async with _get_pg_pools_lock():
        # #001: if the loop changed since these pools were built, they belong
        # to a now-closed loop (asyncio.run() per turn). Their transports were
        # already torn down when that loop closed, so we cannot await
        # pool.close() on the dead loop — just drop the references and rebuild
        # on the current loop. Without this, the cached pool below would be
        # reused on the new loop and crash on first use.
        if _pg_pools_loop is not loop:
            _pg_pools.clear()
            _pg_pools_loop = loop
        pool = _pg_pools.get(connection)
        if pool is None:
            import asyncpg
            parsed = urlparse(connection)
            hostname = parsed.hostname or ""
            ssl_ctx = _build_pg_ssl_context(connection)

            # Loopback nao precisa de IP-pinning (sem rebinding window real).
            # Para hosts remotos, pin do IP previne rebinding no connect.
            kwargs: dict = {
                "min_size": 1,
                "max_size": 5,
                "command_timeout": 30,
                "ssl": ssl_ctx,
            }
            if hostname.lower() in _LOOPBACK_HOSTS:
                pool = await asyncpg.create_pool(connection, **kwargs)
            else:
                # `_resolve_and_validate` retorna IP publico apos validar
                # SSRF; raise ValueError se privado/falha. A SSRF check ja
                # rodou em `_validate_pg_ssrf` (caller) — aqui re-resolvemos
                # explicitamente para PINAR o IP que asyncpg usara, evitando
                # nova resolucao DNS no connect.
                ip = await _resolve_and_validate(hostname)
                # Reconstroi kwargs no formato keyword (asyncpg aceita
                # connection string + overrides; passar host/hostaddr
                # como kwargs sobrepoe os derivados do dsn).
                pool = await asyncpg.create_pool(
                    host=hostname,
                    hostaddr=ip,
                    port=parsed.port or 5432,
                    user=parsed.username,
                    password=parsed.password,
                    database=(parsed.path or "/").lstrip("/") or None,
                    **kwargs,
                )
            _pg_pools[connection] = pool
    return pool


async def _acquire_pg_pool(connection: str) -> tuple[object, float] | dict:
    """Validate SSRF, check asyncpg import, and acquire a PG pool connection.

    Returns (pool, fetch_timeout) on success, or an error dict on failure.
    Extracted from _query_database and _describe_table (#DM059 / AUDIT_V1.10 #053).
    """
    ssrf_error = await _validate_pg_ssrf(connection)
    if ssrf_error is not None:
        return ssrf_error

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return {"error": "asyncpg não instalado. Execute: pip install asyncpg"}

    fetch_timeout = TOOL_TIMEOUTS.get("database", 30)
    try:
        pool = await asyncio.wait_for(_get_pg_pool(connection), timeout=10)
    except asyncio.TimeoutError:
        return {"error": "Timeout ao conectar ao PostgreSQL", "timeout": True}
    return pool, fetch_timeout


async def _close_pg_pools() -> None:
    """Cleanup helper — chamar em shutdown da CLI."""
    global _pg_pools_loop
    pools = list(_pg_pools.values())
    _pg_pools.clear()
    _pg_pools_loop = None  # #001: cache emptied — next get() rebinds to its loop
    for pool in pools:
        try:
            await asyncio.wait_for(pool.close(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("asyncpg pool close timeout — possible connection leak")
        except Exception:
            # AUDIT_V1.8 #036: pool.close() failing silently esconde
            # leaks. logger.exception preserva o stacktrace pra diagnostico.
            logger.exception("asyncpg pool close failed")

# SQL statements that modify data
_WRITE_PATTERNS = re.compile(
    r"^\s*("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|MERGE"
    r"|VACUUM|REINDEX|ANALYZE|GRANT|REVOKE|SET\s+ROLE|RESET"
    r")\b",
    re.IGNORECASE,
)

# Dangerous patterns always blocked (even in non-readonly mode)
_BLOCKED_SQL = [
    r"\bATTACH\b",  # attach external databases
    r"\bDETACH\b",  # detach databases
    r"\bLOAD_EXTENSION\b",  # load extensions
    r"\bPRAGMA\s+.*=",  # write pragmas (read pragmas OK)
    r"\bCOPY\s+.*\s+TO\s+PROGRAM\b",  # shell execution via PG (#020)
    r"\bCOPY\s+.*\s+FROM\s+PROGRAM\b",  # shell execution via PG (#020)
]


# Regex para validar nomes de tabela seguros (previne SQL injection em PRAGMA/describe)
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _is_write_query(query: str) -> bool:
    """Check if query modifies data."""
    return bool(_WRITE_PATTERNS.match(query.strip()))


def _is_dangerous_query(sql: str) -> str | None:
    """Retorna mensagem de erro se a query é perigosa, None se OK.

    Multi-statement detector (#030): segue o SQL standard onde a unica
    forma de escapar uma quote dentro de string e doubled-quote (`''`).
    O backslash (`\\'`) NAO escapa em SQL standard (Postgres com
    standard_conforming_strings=on, default desde 9.1; SQLite idem).
    O detector legacy tratava `\\'` como escape, criando bypass via
    `'a\\'; DROP TABLE t; --` (que em SQL standard fecha a string em
    `'a\\'` e abre multi-statement).
    """
    stripped = sql.strip()

    # Bloquear multi-statement (;) fora de strings
    in_string = False
    quote_char = None
    i = 0
    n = len(stripped)
    while i < n:
        c = stripped[i]
        if not in_string:
            if c in ("'", '"'):
                in_string, quote_char = True, c
            elif c == ";" and i < n - 1:
                remaining = stripped[i + 1:].strip()
                if not remaining:
                    i += 1
                    continue
                # AUDIT_V1.8 #009: comentario `--` apos `;` mascara
                # multi-statement no detector. `WHERE 1; -- comment\nDROP t`
                # passa porque remaining.startswith("--") → True.
                # Precisa pular a linha do comentario e reavaliar.
                if remaining.startswith("--"):
                    nl = remaining.find("\n")
                    after = remaining[nl + 1:].strip() if nl != -1 else ""
                    if after:
                        return "Multi-statement queries bloqueadas por segurança"
                    i += 1
                    continue
                if remaining.startswith("/*"):
                    end = remaining.find("*/")
                    if end == -1:
                        return "Multi-statement com block-comment nao-terminado"
                    after = remaining[end + 2:].strip()
                    if after:
                        return "Multi-statement queries bloqueadas por segurança"
                    i += 1
                    continue
                return "Multi-statement queries bloqueadas por segurança"
        else:
            # Dentro de string: doubled-quote escapa, qualquer outra ocorrencia
            # da mesma quote fecha. Backslash NAO escapa (SQL standard).
            if c == quote_char:
                if i + 1 < n and stripped[i + 1] == quote_char:
                    i += 2  # skip pair, ainda em string
                    continue
                in_string = False
        i += 1

    # Bloquear CTE + write (WITH ... DELETE/UPDATE/INSERT)
    if re.match(r"\s*WITH\b", stripped, re.I):
        if re.search(r"\b(DELETE|UPDATE|INSERT|DROP|ALTER|TRUNCATE)\b", stripped, re.I):
            return "CTE com operação de escrita requer aprovação"

    return None


def _validate_query(query: str, read_only: bool) -> str | None:
    """Validate SQL query. Returns error message or None."""
    for pattern in _BLOCKED_SQL:
        if re.search(pattern, query, re.IGNORECASE):
            return "Query bloqueada por segurança: padrão perigoso detectado"

    # Check multi-statement and dangerous patterns
    danger = _is_dangerous_query(query)
    if danger:
        return danger

    if read_only and _is_write_query(query):
        return (
            "Query de escrita bloqueada em modo read_only. "
            "Use read_only=false para permitir escrita."
        )

    return None


def _validate_sqlite_path(db_path: str) -> str | None:
    """Ensure SQLite path is within workspace."""
    p = Path(db_path).expanduser().resolve()
    err = assert_within_workspace(p)
    if err:
        return err
    if not p.exists():
        return f"Arquivo de banco de dados não encontrado: {db_path}"
    return None


async def _query_sqlite(db_path: str, query: str, read_only: bool) -> dict:
    """Execute query on SQLite database."""
    path_error = _validate_sqlite_path(db_path)
    if path_error:
        return {"error": path_error}

    conn_box: dict = {}

    def _execute():
        # Use URI mode=ro for true read-only enforcement (cannot be bypassed)
        if read_only:
            resolved = Path(db_path).expanduser().resolve()
            # #D024: filenames com `?` ou `#` (validos em Linux/macOS)
            # quebrariam a URI sem url-encoding — segundo `?` vira query
            # continuation, `#` vira fragment. quote preserva `/` por default.
            uri = f"file:{quote(str(resolved))}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(db_path)
        conn_box["conn"] = conn
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(query)

            if _is_write_query(query):
                conn.commit()
                return {
                    "rows_affected": cursor.rowcount,
                    "last_row_id": cursor.lastrowid,
                    "query": query,
                }

            rows = cursor.fetchmany(_MAX_ROWS)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []

            result_rows = [dict(row) for row in rows]
            total = len(result_rows)

            return {
                "columns": columns,
                "rows": result_rows,
                "row_count": total,
                "truncated": total >= _MAX_ROWS,
                "query": query,
            }
        except sqlite3.Error as e:
            return {"error": f"SQLite error: {e}", "query": query}
        finally:
            conn.close()

    # #D005: sem `wait_for`, uma query SQLite que trava (lock contention,
    # corrupted page) pendurava o agent indefinidamente. Cap de 30s alinha
    # com TOOL_TIMEOUTS["database"].
    # #D804: o timeout bounda o RETORNO do tool; a thread so e liberada de
    # fato pelo `conn.interrupt()` abaixo (aborta o cursor.execute em curso).
    # Executor dedicado evita vazar workers do pool default compartilhado.
    db_timeout = TOOL_TIMEOUTS.get("database", 30)
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_get_sqlite_executor(), _execute),
            timeout=db_timeout,
        )
    except TimeoutError:
        conn = conn_box.get("conn")
        if conn is not None:
            try:
                conn.interrupt()
            except Exception:
                pass
        return {
            "error": f"SQLite query excedeu timeout de {db_timeout}s",
            "timeout": True,
            "query": query,
        }


async def _validate_pg_ssrf(connection: str) -> dict | None:
    """SSRF guard for PostgreSQL connection strings (#D013 V1.0).

    Returns an error dict if the connection targets a private/internal IP,
    None if the connection passes. Helper extracted from the duplicated
    inline check that lived in `_query_database` and `_describe_table`.

    AUDIT_V1.2 #002: a versao sincrona chamava `_is_private_ip` que faz
    `socket.getaddrinfo` bloqueante (ate 5s no DNS timeout). Em async
    function isso bloqueia o event loop inteiro — tasks paralelas param,
    UI de approval congela, loop detection pode disparar falsamente.
    `asyncio.to_thread` move a resolucao DNS para o thread pool default.
    """
    try:
        parsed_url = urlparse(connection)
        pg_hostname = parsed_url.hostname
        if pg_hostname:
            is_private = await asyncio.to_thread(_is_private_ip, pg_hostname)
            if is_private:
                return {
                    "error": (
                        f"Conexão a IP privado/interno bloqueada por segurança "
                        f"(SSRF protection): {pg_hostname}"
                    ),
                    "blocked": True,
                }
    except Exception as e:
        # #037: registrar o motivo real (parse de DSN malformado). DNS-errors
        # NAO caem aqui — `is_private_ip` é fail-closed (resolução falha →
        # retorna True → bloqueado como SSRF, acima), então este ramo cobre só
        # connection strings malformadas. Sem o log, a causa ficava invisível.
        logger.debug("PG SSRF: connection string parse failed: %s", e, exc_info=True)
        return {"error": "Connection string inválida para PostgreSQL"}
    return None


async def _query_database(
    connection: str,
    query: str,
    read_only: bool = True,
    db_type: str = "sqlite",
) -> dict:
    """Execute a SQL query on a local database."""
    # Validate query
    query_error = _validate_query(query, read_only)
    if query_error:
        return {"error": query_error, "blocked": True}

    if db_type == "sqlite":
        return await _query_sqlite(connection, query, read_only)

    elif db_type == "postgresql":
        result = await _acquire_pg_pool(connection)
        if isinstance(result, dict):
            return result
        pool, fetch_timeout = result

        try:
            async with pool.acquire(timeout=fetch_timeout) as conn:
                if _is_write_query(query):
                    if read_only:
                        return {"error": "Query de escrita bloqueada em modo read_only"}
                    result = await asyncio.wait_for(
                        conn.execute(query), timeout=fetch_timeout
                    )
                    return {"result": result, "query": query}
                rows = await asyncio.wait_for(
                    conn.fetch(query), timeout=fetch_timeout
                )
                rows_list = [dict(r) for r in rows[:_MAX_ROWS]]
                columns = list(rows_list[0].keys()) if rows_list else []
                return {
                    "columns": columns,
                    "rows": rows_list,
                    "row_count": len(rows_list),
                    "truncated": len(rows) >= _MAX_ROWS,
                    "query": query,
                }
        except TimeoutError:
            return {
                "error": f"PostgreSQL operation excedeu timeout de {fetch_timeout}s",
                "timeout": True,
            }
        except Exception as e:
            # asyncpg errors podem incluir o DSN com password no str(e).
            return {"error": sanitize_for_log(f"PostgreSQL error: {e}")}

    return {"error": f"Tipo de banco '{db_type}' não suportado. Use 'sqlite' ou 'postgresql'."}


async def _list_tables(connection: str, db_type: str = "sqlite") -> dict:
    """List all tables in a database."""
    if db_type == "sqlite":
        return await _query_sqlite(
            connection,
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name",
            read_only=True,
        )
    elif db_type == "postgresql":
        return await _query_database(
            connection,
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name",
            read_only=True,
            db_type="postgresql",
        )
    return {"error": f"Tipo de banco '{db_type}' não suportado"}


async def _describe_table(connection: str, table: str, db_type: str = "sqlite") -> dict:
    """Describe a table's schema."""
    # Validar nome da tabela contra SQL injection
    if not _SAFE_IDENTIFIER.match(table):
        return {"error": f"Nome de tabela inválido: '{table}'"}

    if db_type == "sqlite":
        return await _query_sqlite(
            connection,
            f"PRAGMA table_info({table})",
            read_only=True,
        )
    elif db_type == "postgresql":
        result = await _acquire_pg_pool(connection)
        if isinstance(result, dict):
            return result
        pool, fetch_timeout = result

        # Usar parameterized query para PostgreSQL
        try:
            async with pool.acquire(timeout=fetch_timeout) as conn:
                rows = await asyncio.wait_for(
                    conn.fetch(
                        "SELECT column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns WHERE table_name = $1 "
                        "ORDER BY ordinal_position",
                        table,
                    ),
                    timeout=fetch_timeout,
                )
                rows_list = [dict(r) for r in rows]
                columns = list(rows_list[0].keys()) if rows_list else []
                return {
                    "columns": columns,
                    "rows": rows_list,
                    "row_count": len(rows_list),
                    # AUDIT_V2.2 #025: report the SQL actually executed, not a
                    # fake `DESCRIBE` (which isn't even valid Postgres).
                    "query": (
                        "SELECT column_name, data_type, is_nullable, "
                        "column_default FROM information_schema.columns "
                        "WHERE table_name = $1 ORDER BY ordinal_position"
                    ),
                }
        except TimeoutError:
            return {"error": "Timeout ao conectar ao PostgreSQL"}
        except Exception as e:
            # asyncpg errors podem incluir o DSN com password no str(e).
            return {"error": sanitize_for_log(f"PostgreSQL error: {e}")}

    return {"error": f"Tipo de banco '{db_type}' não suportado"}


register_tool(
    ToolDefinition(
        name="query_database",
        description=(
            "Execute a SQL query on a local database (SQLite or PostgreSQL). "
            "read_only mode by default — write queries need read_only=false. "
            "Limited to 500 rows per result. ATTACH and LOAD_EXTENSION are blocked."
        ),
        parameters={
            "type": "object",
            "properties": {
                "connection": {
                    "type": "string",
                    "description": "For SQLite: path to the .db/.sqlite file. For PostgreSQL: connection string",
                },
                "query": {
                    "type": "string",
                    "description": "SQL query to execute",
                },
                "read_only": {
                    "type": "boolean",
                    "description": "Read-only mode (blocks INSERT/UPDATE/DELETE). Default: true",
                    "default": True,
                },
                "db_type": {
                    "type": "string",
                    "description": "Database type",
                    "enum": ["sqlite", "postgresql"],
                    "default": "sqlite",
                },
            },
            "required": ["connection", "query"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.DATABASE,
        executor=_query_database,
    )
)

register_tool(
    ToolDefinition(
        name="list_tables",
        description="List all tables and views in a database.",
        parameters={
            "type": "object",
            "properties": {
                "connection": {
                    "type": "string",
                    "description": "For SQLite: path to the .db file. For PostgreSQL: connection string",
                },
                "db_type": {
                    "type": "string",
                    "description": "Database type",
                    "enum": ["sqlite", "postgresql"],
                    "default": "sqlite",
                },
            },
            "required": ["connection"],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.DATABASE,
        executor=_list_tables,
    )
)

register_tool(
    ToolDefinition(
        name="describe_table",
        description="Describe a table's schema (columns, types, constraints).",
        parameters={
            "type": "object",
            "properties": {
                "connection": {
                    "type": "string",
                    "description": "For SQLite: path to the .db file. For PostgreSQL: connection string",
                },
                "table": {
                    "type": "string",
                    "description": "Table name",
                },
                "db_type": {
                    "type": "string",
                    "description": "Database type",
                    "enum": ["sqlite", "postgresql"],
                    "default": "sqlite",
                },
            },
            "required": ["connection", "table"],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.DATABASE,
        executor=_describe_table,
    )
)
