"""Process lifecycle hooks for the CLI.

Owns the atexit registrations and the SIGTERM handler. Keeping this in
one place makes shutdown order auditable: cleanup_temp_images,
shutdown_browser, shutdown_mcp_servers, on_stop hooks. SIGTERM exits
via ``sys.exit`` so atexit fires the same handlers — without this,
``kill <pid>`` (default SIGTERM, e.g. systemd timeout) would leave
orphan browser runtimes and unsaved sessions.

Public entry: ``install_lifecycle_hooks()`` — called once from ``main()``.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sys

from alpha import hooks
from alpha.mcp import shutdown_mcp_servers
from alpha.repl_input import cleanup_temp_images


def _shutdown_browser_session() -> None:
    """atexit hook: close any persistent browser session.

    When called from a running event loop (tests, embedding), schedule
    the shutdown as a task instead of trying to create a new loop.
    """
    try:
        from alpha.tools.browser_session import shutdown_browser

        try:
            running = asyncio.get_running_loop()
            running.create_task(shutdown_browser())
        except RuntimeError:
            asyncio.run(shutdown_browser())
    except ImportError:
        pass  # Playwright nao instalado, sem session pra fechar
    except Exception as e:
        try:
            print(
                f"shutdown_browser_session: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
        except Exception:
            pass


def _shutdown_mcp_servers() -> None:
    """atexit hook: terminate any spawned MCP server subprocesses."""
    try:
        shutdown_mcp_servers()
    except Exception:
        pass


def _fire_on_stop() -> None:
    """atexit hook: fire user-defined on_stop hooks."""
    try:
        hooks.fire("on_stop", workspace=os.getcwd())
    except Exception:
        pass


def _install_sigterm_handler() -> None:
    """Trigger atexit cleanup on SIGTERM (#067).

    Sem isto, `kill <pid>` (default SIGTERM, ex: container shutdown,
    systemd timeout) mata o processo SEM rodar atexit hooks: browser
    runtime fica zumbi, sessao nao salva, MCP servers ficam orfaos.
    `signal.signal(SIGTERM, ...)` faz o handler sair via `sys.exit`,
    o que dispara os atexit. Em SO sem SIGTERM (Windows nativo) e
    no-op silencioso.
    """
    import signal as _signal
    if not hasattr(_signal, "SIGTERM"):
        return

    def _on_sigterm(signum, frame):
        try:
            print("\n[ALPHA] SIGTERM received — running cleanup", file=sys.stderr)
        except Exception:
            pass
        sys.exit(143)  # 128 + 15 (SIGTERM), Unix convention

    _signal.signal(_signal.SIGTERM, _on_sigterm)


_INSTALLED = False


def install_lifecycle_hooks() -> None:
    """Register atexit + SIGTERM handlers. Idempotent.

    Order matters: ``on_stop`` user hooks fire FIRST so the user's audit
    log sees the session end before browser/MCP teardown emit their own
    output.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    # Indicator first so its scroll-region tear-down runs before other
    # output (browser/MCP shutdown messages) — otherwise terminal stays
    # clipped when the agent crashes mid-stream.
    from alpha.display import cleanup_indicator
    atexit.register(cleanup_indicator)
    atexit.register(_fire_on_stop)
    atexit.register(_shutdown_browser_session)
    atexit.register(_shutdown_mcp_servers)
    atexit.register(cleanup_temp_images)
    _install_sigterm_handler()
    _INSTALLED = True
