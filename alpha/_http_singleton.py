"""Loop-aware shared httpx.AsyncClient (#DM042 / closes #DM032).

httpx.AsyncClient amarra o transport ao loop em que foi criado. Quando o
CLI roda em modo single-shot (`python main.py "task"`), cada chamada de
`asyncio.run()` cria um loop novo mas o modulo persiste em cache de
imports (em testes, daemon mode, ou reuso de processo). Sem detectar
isso, a proxima request crasha com `RuntimeError: Event loop is closed`.

Este modulo encapsula o padrao que estava duplicado em `llm.py`,
`llm_anthropic.py` e `web_search.py` (cada um com pequenas variacoes —
lock ausente em web_search era bug latente em concorrencia).

Uso:

    _HTTP = LoopAwareClient(
        name="llm",
        build=lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(LLM_TIMEOUT, connect=10.0),
        ),
    )
    client = await _HTTP.get()

`build` e chamado *cada vez* que precisa criar um cliente novo (loop
trocado ou client fechado). Mantenha-o cheap: capture configs por
closure, nao crie state global.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)


class LoopAwareClient:
    """Shared httpx.AsyncClient que rebuilda quando o event loop troca.

    Double-check lock protege a janela aclose+reassign contra coroutines
    concorrentes criando clientes duplicados (AUDIT_V1.2 #006). Web_search
    nao tinha lock — adotar `LoopAwareClient` corrige isso de graca.

    Idempotente: `get()` e seguro de chamar de multiplos lugares; `close()`
    pode rodar mais de uma vez (atexit hooks, test teardown).
    """

    __slots__ = ("_name", "_build", "_client", "_loop", "_lock")

    def __init__(self, name: str, build: Callable[[], httpx.AsyncClient]):
        self._name = name
        self._build = build
        self._client: httpx.AsyncClient | None = None
        self._loop: object | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> httpx.AsyncClient:
        """Return the shared client, rebuilding if loop changed or closed."""
        loop = asyncio.get_running_loop()
        # Fast path: no lock when healthy and loop matches.
        if (
            self._client is not None
            and not self._client.is_closed
            and self._loop is loop
        ):
            return self._client
        async with self._lock:
            # Double-check after lock — another coroutine may have rebuilt
            # while we waited.
            if (
                self._client is not None
                and not self._client.is_closed
                and self._loop is loop
            ):
                return self._client
            if self._client is not None and not self._client.is_closed:
                try:
                    await self._client.aclose()
                except Exception as e:
                    logger.debug("aclose stale %s client: %s", self._name, e)
            self._client = self._build()
            self._loop = loop
        return self._client

    async def close(self) -> None:
        """Idempotent close — safe to call from atexit / test teardown."""
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("aclose %s client: %s", self._name, e)
        self._client = None
        self._loop = None

    def is_open(self) -> bool:
        """Cheap introspection (used by tests + shutdown ordering)."""
        return self._client is not None and not self._client.is_closed
