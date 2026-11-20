"""
Persistent browser session for Mythos.

Holds a single Playwright browser instance shared across all browser_* tools
so cookies, login state, and tab history survive between tool calls.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util as _ilu
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# AUDIT_V1.8 #040: Playwright e importado eagerly via try/except no topo
# do modulo — o `try` so trata ImportError, mas se a lib esta instalada o
# import roda em TODA invocacao do CLI (200-500ms cold-load de playwright
# inteiro). Usuario com install minimal mas playwright instalado no venv
# compartilhado paga sem nunca usar browser_*.
#
# Fix: detectar disponibilidade via `importlib.util.find_spec` (estatico,
# sem importar) e fazer o real-import lazy na primeira chamada de
# `_ensure_playwright_loaded()` — chamada apenas pelo `BrowserSession.start()`.
PLAYWRIGHT_AVAILABLE = _ilu.find_spec("playwright") is not None

# #DM077: mensagem única para Playwright ausente/corrompido — antes copiada
# em 3 sites (aqui e em browser_tools._check_available), que podiam driftar.
PLAYWRIGHT_INSTALL_MSG = (
    "Playwright not installed. Run: "
    "pip install playwright && playwright install chromium"
)

# Placeholders nao-importados. `_ensure_playwright_loaded()` substitui esses
# nomes pelos symbols reais na primeira invocacao das browser tools.
Browser = BrowserContext = Page = Playwright = async_playwright = None  # type: ignore


def _ensure_playwright_loaded():
    """Lazy import dos symbols Playwright. Idempotente.

    Chamada pelos sites que realmente usam Browser/BrowserContext/Page/
    async_playwright — apenas dentro de `BrowserSession.start()` e do
    bloco que detecta isinstance(). Se Playwright nao esta instalado OU
    instalado mas com sub-dep quebrada, retorna False (caller deve fall
    back para mensagem de erro amigavel).

    AUDIT_V1.8 #R202: `importlib.util.find_spec("playwright")` so verifica
    se o top-level package existe — nao executa o import. Se `playwright`
    esta instalado mas com sub-dependencia quebrada (greenlet ausente,
    ABI mismatch em playwright._impl, wheel parcial, distro upgrade
    incompleto), o real `from playwright.async_api import ...` aqui dispara
    ImportError. Sem try/except, esse traceback cru propaga ate o caller
    em vez do `RuntimeError("Playwright not installed...")` amigavel.
    Wrap garante que ambos os modos de falha (nao-instalado vs corrupto)
    retornem False — caller exibe a mesma mensagem actionable.
    """
    global Browser, BrowserContext, Page, Playwright, async_playwright
    if not PLAYWRIGHT_AVAILABLE:
        return False
    if async_playwright is not None:
        return True  # ja carregado
    try:
        from playwright.async_api import (  # noqa: I001
            Browser as _B,
            BrowserContext as _BC,
            Page as _P,
            Playwright as _PW,
            async_playwright as _ap,
        )
    except ImportError as e:
        # find_spec disse OK mas import real falhou — install corrupto.
        # Log para diagnose (usuario vai ver "not installed" do caller).
        logger.warning(
            "Playwright top-level present but sub-import failed: %s "
            "(install may be corrupt — try: pip install --force-reinstall playwright)",
            e,
        )
        return False
    Browser = _B
    BrowserContext = _BC
    Page = _P
    Playwright = _PW
    async_playwright = _ap
    return True


_BLOCKED_SCHEMES = frozenset(
    {"file", "chrome", "chrome-extension", "about", "javascript", "data", "view-source"}
)

# #DM075: kwargs de criação de contexto compartilhados entre os dois ramos
# (perfil persistente × contexto isolado) — antes duplicados em lockstep.
_VIEWPORT = {"width": 1280, "height": 800}
_CONTEXT_KWARGS = {
    "viewport": _VIEWPORT,
    "accept_downloads": False,
    "java_script_enabled": True,
}


# ─── Dedicated browser event loop (Plano-Browser Etapa 2) ────────────
#
# O CLI roda `asyncio.run()` por turno — loop novo a cada turno — e objetos
# Playwright são atrelados ao loop onde nasceram. Antes, a sessão era
# descartada a cada turno (#002: bounded leak + cookies perdidos). Agora
# TODAS as operações de browser rodam num event loop dedicado que vive numa
# thread daemon própria: o loop nunca muda, então a mesma sessão (cookies,
# abas, login) sobrevive a conversa inteira. O mecanismo loop-aware de
# `is_open()`/`_discard_stale_session` permanece como defesa em profundidade
# para chamadas diretas fora do dispatcher (testes, uso embutido).


class _BrowserLoop:
    """Loop asyncio dedicado ao browser, numa thread daemon lazy-started."""

    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _start_lock = threading.Lock()

    @classmethod
    def loop(cls) -> asyncio.AbstractEventLoop:
        with cls._start_lock:
            if cls.alive():
                return cls._loop  # type: ignore[return-value]  # alive() garante não-None
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            t = threading.Thread(target=_run, name="alpha-browser-loop", daemon=True)
            t.start()
            if not ready.wait(timeout=5):
                raise RuntimeError("Browser loop thread failed to start")
            cls._thread, cls._loop = t, loop
            return loop

    @classmethod
    def alive(cls) -> bool:
        return (
            cls._loop is not None
            and cls._thread is not None
            and cls._thread.is_alive()
        )

    @classmethod
    def submit(cls, coro):
        """Agenda um coroutine no loop do browser → concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, cls.loop())

    @classmethod
    def stop(cls) -> None:
        with cls._start_lock:
            if cls.alive():
                cls._loop.call_soon_threadsafe(cls._loop.stop)
                cls._thread.join(timeout=3)
            cls._thread = None
            cls._loop = None


def run_on_browser_loop(fn):
    """Decorator: executa o coroutine-fn no loop dedicado do browser.

    Aplicado aos executors das tools browser_* na registração — o corpo da
    tool inteiro (require_page + operações Playwright) roda no loop
    persistente. Reentrante: se já estamos no loop do browser (tool composta
    chamando outra browser tool), executa inline sem re-despachar.
    """
    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        try:
            if asyncio.get_running_loop() is _BrowserLoop._loop:
                return await fn(*args, **kwargs)
        except RuntimeError:
            pass
        fut = _BrowserLoop.submit(fn(*args, **kwargs))
        return await asyncio.wrap_future(fut)

    return _wrapper


def _profile_dir() -> Path:
    """Diretório do perfil persistente (cookies/logins reais → 0700)."""
    raw = os.environ.get("ALPHA_BROWSER_PROFILE_DIR", "").strip() or "~/.alpha/browser-profile"
    d = Path(raw).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError as e:
        logger.warning("Could not chmod 700 browser profile dir: %s", e)
    return d


# `urlparse(...).hostname` strips IPv6 brackets, so "[::1]" never appears here.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def allowlist_configured() -> bool:
    """True when the domain allowlist actually enforced by `_domain_allowed`
    is non-empty (single source: `config.BROWSER_DOMAIN_ALLOWLIST`)."""
    from alpha.config import BROWSER_DOMAIN_ALLOWLIST
    return bool(BROWSER_DOMAIN_ALLOWLIST)


def cdp_endpoint(explicit: str | None = None) -> str | None:
    """Resolve the CDP endpoint for *attach* mode (`ALPHA_BROWSER_CDP`).

    Only the user opts in (env var); the model has no way to pass an
    endpoint — attaching hands the agent a browser full of real logins.
    The endpoint must be loopback unless `ALPHA_BROWSER_CDP_ALLOW_REMOTE=1`.
    Accepts `http(s)://host:port` or `ws(s)://…`; a bare `host:port` or a
    bare port become `http://…`; IPv6 needs brackets (`[::1]:9222`).
    `explicit` exists for /doctor and tests. Raises ValueError when rejected.
    """
    raw = (explicit if explicit is not None else os.environ.get("ALPHA_BROWSER_CDP", "")).strip()
    if not raw:
        return None
    if raw.isdigit():
        raw = f"http://127.0.0.1:{raw}"
    elif "://" not in raw:
        if raw.count(":") > 1 and not raw.startswith("["):
            raise ValueError(
                f"ALPHA_BROWSER_CDP inválido: {raw!r} — IPv6 precisa de colchetes, ex. [::1]:9222"
            )
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        parsed.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError as e:
        raise ValueError(f"ALPHA_BROWSER_CDP inválido: {raw!r} ({e})") from e
    if parsed.scheme not in ("http", "https", "ws", "wss") or not host:
        raise ValueError(f"ALPHA_BROWSER_CDP inválido: {raw!r} (use http://127.0.0.1:9222)")
    allow_remote = os.environ.get("ALPHA_BROWSER_CDP_ALLOW_REMOTE", "").strip().lower() in ("1", "true", "yes", "on")
    if parsed.hostname not in _LOOPBACK_HOSTS and not allow_remote:
        raise ValueError(
            f"ALPHA_BROWSER_CDP aponta para host não-local ({parsed.hostname}); "
            "a porta de depuração dá controle total do browser. "
            "Defina ALPHA_BROWSER_CDP_ALLOW_REMOTE=1 se for intencional."
        )
    return raw


class BrowserSession:
    """Singleton Playwright session reused across tool calls."""

    _instance: "BrowserSession | None" = None
    # Lock criado lazy. Antes era `_lock = asyncio.Lock()` no escopo da
    # classe (avaliado no module-load), atrelando-se ao primeiro event
    # loop que tocasse o atributo. O CLI roda asyncio.run() por turn —
    # loop novo cada vez — disparando `RuntimeError: attached to a
    # different loop` na 2a turn. Mesmo padrao de alpha/llm.py.
    _lock: "asyncio.Lock | None" = None
    _lock_loop: object | None = None

    def __init__(self):
        self.playwright: "Playwright | None" = None
        self.browser: "Browser | None" = None
        self.context: "BrowserContext | None" = None
        self.pages: list = []
        self.active_idx: int = 0
        self.headless: bool = True
        # Etapa 2: modo perfil persistente (launch_persistent_context).
        # Nesse modo `self.browser` é None — o context É o browser.
        self.persistent: bool = False
        self.profile_dir: str | None = None
        # Attach mode: connected over CDP to a browser the USER started
        # (their real Chrome). We never launched it, so close() only
        # disconnects. `cdp` is the endpoint we are attached to.
        self.attached: bool = False
        self.cdp: str | None = None
        # Etapa 4: ring buffers de observabilidade (console + network),
        # alimentados por listeners page.on — caps fixos contra vazamento
        # de memória em sessões longas.
        self.console_logs: deque = deque(maxlen=500)
        self.network_events: deque = deque(maxlen=500)
        # #D042: keep strong refs to fire-and-forget rescue tasks so they
        # aren't GC'd before they navigate a malicious page away.
        self._rescue_tasks: set = set()
        # #002: the event loop this session's playwright/browser/context are
        # bound to. The CLI runs asyncio.run() per turn (new loop each turn);
        # reusing the singleton's browser on a new loop raised "attached to a
        # different loop". Tracked so `is_open()` reports stale and `open()`
        # rebuilds on the current loop.
        self._loop: object | None = None

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if cls._lock is None or cls._lock_loop is not loop:
            cls._lock = asyncio.Lock()
            cls._lock_loop = loop
        return cls._lock

    @classmethod
    async def get(cls) -> "BrowserSession":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def page(self):
        if not self.pages:
            return None
        if self.active_idx >= len(self.pages):
            self.active_idx = 0
        return self.pages[self.active_idx]

    def is_open(self) -> bool:
        # #002: a session bound to a previous (now-closed) loop is unusable —
        # report not-open so the caller rebuilds on the current loop.
        try:
            if self._loop is not None and self._loop is not asyncio.get_running_loop():
                return False
        except RuntimeError:
            pass  # no running loop (sync context) — fall back to conn check
        if self.browser is not None:
            return self.browser.is_connected()
        # Persistent context (Etapa 2): não há objeto Browser — o context
        # existe enquanto a sessão vive; operações numa sessão morta falham
        # na tool e são reportadas como erro normal.
        return self.persistent and self.context is not None

    def _discard_stale_session(self) -> None:
        """#002: drop refs to a session bound to a now-closed loop. We can't
        await `close()` on the dead loop, so the previous playwright/browser
        process may be orphaned until process exit — a bounded leak, far better
        than the crash that reusing the stale session caused."""
        if self.browser is not None:
            logger.warning(
                "Browser session was bound to a closed event loop — rebuilding; "
                "the previous browser process may linger until exit."
            )
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages = []
        self.active_idx = 0
        self.attached = False
        self.cdp = None
        self._rescue_tasks.clear()
        self._loop = None

    def same_mode(self, headless: bool, profile: bool, cdp: str | None) -> bool:
        """True when an open session already matches the requested mode."""
        if cdp:
            return self.attached and self.cdp == cdp
        return not self.attached and profile == self.persistent and headless == self.headless

    async def open(self, headless: bool = True, profile: bool = False,
                   cdp: str | None = None) -> None:
        """Open (or reuse) the session.

        `cdp` = endpoint of a browser the user already runs with
        `--remote-debugging-port` → attach instead of launching; `headless`
        and `profile` are ignored in that mode (the browser is theirs)."""
        async with self._get_lock():
            # #002: if the loop changed since this session was opened, the old
            # browser/context are unusable on the new loop — discard and rebuild.
            loop = asyncio.get_running_loop()
            if self._loop is not None and self._loop is not loop:
                self._discard_stale_session()
            if self.is_open():
                if self.same_mode(headless, profile, cdp):
                    return
                # Mudança de modo (perfil↔isolado, headless↔headed, attach):
                # fecha a sessão atual e reconstrói. _close_locked porque o
                # lock já é nosso (asyncio.Lock não é reentrante).
                logger.info(
                    "Browser mode change (profile=%s→%s, headless=%s→%s, cdp=%s→%s) — reopening",
                    self.persistent, profile, self.headless, headless, self.cdp, cdp,
                )
                await self._close_locked()
            # AUDIT_V1.8 #040: lazy import dos symbols Playwright apenas
            # quando o user realmente vai abrir browser. Ate aqui, so
            # `find_spec` rodou no module-load (cheap, sem cold-load).
            # #DM077: cobre também PLAYWRIGHT_AVAILABLE=False (retorna False
            # imediatamente) — o guard separado que existia aqui era redundante.
            if not _ensure_playwright_loaded():
                raise RuntimeError(PLAYWRIGHT_INSTALL_MSG)
            # Constroi tudo em locais antes de atribuir a self — se launch()
            # ou new_context() falhar, paramos o playwright e a instancia
            # fica num estado limpo. Sem isto, falhas de launch acumulam
            # 1 runtime por tentativa (combina com #054 close-leak).
            pw = await async_playwright().start()
            profile_dir: str | None = None
            try:
                if cdp:
                    # Attach to the user's own browser. We use its default
                    # context (that is where their cookies/logins live) but
                    # open OUR OWN tab and never adopt or listen to theirs:
                    # their tabs stay outside the navigation guard, the tab
                    # tools, the read tools and the allowlist bypass.
                    if not allowlist_configured():
                        logger.warning(
                            "Attached to the user's browser over CDP WITHOUT a domain "
                            "allowlist — the agent can reach any site they are logged "
                            "into. Considere ALPHA_BROWSER_ALLOWLIST."
                        )
                    browser = await pw.chromium.connect_over_cdp(cdp)
                    contexts = list(getattr(browser, "contexts", []) or [])
                    context = contexts[0] if contexts else await browser.new_context(**_CONTEXT_KWARGS)
                    page = await context.new_page()
                elif profile:
                    # Etapa 2: perfil persistente — cookies/logins sobrevivem
                    # entre sessões do Alpha. Sem user_agent custom: em fluxo
                    # de login o browser deve parecer normal (UA custom quebra
                    # sites e dispara anti-bot).
                    pdir = _profile_dir()
                    profile_dir = str(pdir)
                    if not allowlist_configured():
                        logger.warning(
                            "Browser profile mode WITHOUT domain allowlist — "
                            "cookies reais + navegação irrestrita ampliam o "
                            "risco de exfiltração. Considere ALPHA_BROWSER_ALLOWLIST."
                        )
                    browser = None
                    context = await pw.chromium.launch_persistent_context(
                        profile_dir,
                        headless=headless,
                        **_CONTEXT_KWARGS,
                    )
                    page = context.pages[0] if context.pages else await context.new_page()
                else:
                    browser = await pw.chromium.launch(headless=headless)
                    context = await browser.new_context(
                        user_agent="ALPHA-Browser/1.0",
                        **_CONTEXT_KWARGS,
                    )
                    page = await context.new_page()
            except Exception:
                try:
                    await pw.stop()
                except Exception as cleanup_err:
                    logger.warning(f"playwright stop failed during cleanup: {cleanup_err}")
                raise
            self.headless = headless if not cdp else False
            self.persistent = profile if not cdp else False
            self.profile_dir = profile_dir
            self.attached = bool(cdp)
            self.cdp = cdp or None
            self.playwright = pw
            self.browser = browser
            self.context = context
            self.pages = []
            self.active_idx = 0
            self._loop = loop  # #002: bind the session to the current loop
            if not self.attached:
                # Our own context: every page in it is ours.
                self.context.on("page", self._on_new_page)
            # In attach mode the context is the USER's: a context-wide "page"
            # listener would adopt every tab they open. Agent-owned tabs are
            # registered explicitly (here, browser_new_tab) and their popups
            # via the per-page "popup" event wired in adopt_page.
            self.adopt_page(page)

    def adopt_page(self, page) -> None:
        """Register a page the agent owns: guard, observability and, in
        attach mode, popup tracking + download refusal."""
        if page in self.pages:
            return
        self.pages.append(page)
        self._wire_navigation_guard(page)
        self._wire_observability(page)
        if self.attached:
            # The user's default context keeps its own download settings
            # (accept_downloads=False cannot be applied to it): cancel
            # downloads started from agent tabs instead.
            def _on_download(download) -> None:
                try:
                    task = asyncio.get_running_loop().create_task(download.cancel())
                    self._rescue_tasks.add(task)
                    task.add_done_callback(self._rescue_tasks.discard)
                except Exception:
                    logger.debug("download cancel failed", exc_info=True)

            try:
                page.on("popup", self._on_new_page)
                page.on("download", _on_download)
            except Exception:
                logger.debug("attach listeners failed", exc_info=True)

    def _on_new_page(self, page) -> None:
        self.adopt_page(page)

    def _wire_observability(self, page) -> None:
        """Etapa 4: listeners de console e network → ring buffers.

        Handlers minúsculos e à prova de exceção — observabilidade nunca
        pode derrubar a navegação.
        """
        def _on_console(msg) -> None:
            try:
                self.console_logs.append({
                    "ts": time.time(),
                    "level": getattr(msg, "type", "") or "",
                    "text": (getattr(msg, "text", "") or "")[:2000],
                    "page_url": getattr(page, "url", ""),
                })
            except Exception:
                logger.debug("console listener error", exc_info=True)

        def _on_response(response) -> None:
            try:
                req = response.request
                self.network_events.append({
                    "ts": time.time(),
                    "method": getattr(req, "method", ""),
                    "url": (getattr(response, "url", "") or "")[:500],
                    "status": getattr(response, "status", None),
                    "content_type": (response.headers or {}).get("content-type", "")[:100],
                })
            except Exception:
                logger.debug("network listener error", exc_info=True)

        def _on_request_failed(request) -> None:
            try:
                self.network_events.append({
                    "ts": time.time(),
                    "method": getattr(request, "method", ""),
                    "url": (getattr(request, "url", "") or "")[:500],
                    "status": None,
                    "failure": getattr(request, "failure", None) or "failed",
                })
            except Exception:
                logger.debug("requestfailed listener error", exc_info=True)

        try:
            page.on("console", _on_console)
            page.on("response", _on_response)
            page.on("requestfailed", _on_request_failed)
        except Exception as e:
            logger.warning("Could not wire observability listeners: %s", e)

    def _on_rescue_done(self, task) -> None:
        """#D042: drop the strong ref and surface any rescue failure (a
        page we could NOT navigate away from is a real security gap)."""
        self._rescue_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("Failed to rescue page from bad URL: %s", exc)

    def _wire_navigation_guard(self, page) -> None:
        """DEEP_SECURITY V3.3 #D120: re-validar URL pos-navegacao.

        `browser_navigate` valida URL inicial (scheme + allowlist + SSRF),
        mas paginas podem navegar via window.location, meta-refresh, ou
        redirect HTTP que o Playwright segue automaticamente. Sem listener
        `framenavigated`, o browser pode terminar em dominio fora da
        allowlist sem o agente saber.

        Fail-safe: ao detectar navegacao para URL nao-permitida, navegamos
        para about:blank descartando a pagina maliciosa. Logamos para
        diagnostico.
        """
        def _on_frame_nav(frame):
            try:
                # So validamos main frame — iframes sao isolados por origin.
                if frame != page.main_frame:
                    return
                url = frame.url
                if not url or url.startswith(("about:", "chrome:", "data:")):
                    return
                err = validate_browser_url(url)
                if err:
                    logger.warning(
                        "browser navigation blocked post-load: %s (%s)", url, err
                    )
                    # #D042: get_event_loop() is deprecated and may create a
                    # NEW loop (the rescue then never runs on the page's loop).
                    # Use get_running_loop() and STORE the task — an un-stored
                    # create_task() can be GC'd before it navigates away.
                    #
                    # SECURITY_TIER0 (#019): the old code returned here when
                    # there was no running loop — leaving the malicious page
                    # loaded (fail-OPEN). The session captured its loop at wiring
                    # time (`self._loop`); when the callback fires without a
                    # running loop, schedule the rescue onto that loop via
                    # `run_coroutine_threadsafe` so the page is still navigated
                    # away (fail-CLOSED).
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(page.goto("about:blank"))
                        self._rescue_tasks.add(task)
                        task.add_done_callback(self._on_rescue_done)
                    except RuntimeError:
                        bound = self._loop
                        if bound is None:
                            logger.error(
                                "framenavigated guard: no running loop and no "
                                "bound loop — CANNOT rescue page from %s", url,
                            )
                            return
                        fut = asyncio.run_coroutine_threadsafe(
                            page.goto("about:blank"), bound  # type: ignore[arg-type]
                        )
                        self._rescue_tasks.add(fut)
                        fut.add_done_callback(self._on_rescue_done)
            except Exception:
                # #D042: a failure in the guard itself is a real defect, not a
                # routine event — log with exc_info instead of a silent debug.
                logger.warning("framenavigated guard error", exc_info=True)

        try:
            page.on("framenavigated", _on_frame_nav)
        except Exception as e:
            logger.warning("Could not wire framenavigated listener: %s", e)

    async def _close_locked(self) -> None:
        """Corpo do close SEM lock e SEM reset de singleton — usado pelo
        `close()` público e pelo rebuild de modo dentro do `open()` (o
        asyncio.Lock não é reentrante)."""
        # #065: remover listener `_on_new_page` antes de fechar o
        # context. Sem isto, mesmo apos browser.close, o callback
        # mantinha referencia para `self` enquanto Playwright runtime
        # nao GC'ava o context — ciclos abre/fecha empilhavam listeners.
        if self.context is not None and not self.attached:
            try:
                self.context.remove_listener("page", self._on_new_page)
            except Exception as e:
                # Playwright pode levantar se o context ja foi descartado
                # — nao impede o close, mas registramos para diagnostico
                # em cenarios de stress (DR013).
                logger.debug("browser close: remove_listener failed (non-fatal): %s", e)
        if self.attached:
            # Close only the tabs the agent opened. browser.close() on a CDP
            # connection just disconnects and drops contexts Playwright
            # created — our tabs live in the user's default context and
            # would otherwise pile up in their Chrome.
            for p in list(self.pages):
                try:
                    await p.close()
                except Exception as e:
                    logger.debug("attach close: page.close failed (non-fatal): %s", e)
        if self.browser:
            # Attached over CDP: this only disconnects — the user's Chrome,
            # their own tabs and logins stay untouched.
            try:
                await self.browser.close()
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
        elif self.persistent and self.context is not None:
            # Persistent context: não há Browser — fechar o context grava o
            # perfil (cookies/logins) no disco.
            try:
                await self.context.close()
            except Exception as e:
                logger.warning(f"Error closing persistent context: {e}")
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.warning(f"Error stopping playwright: {e}")
        self.browser = None
        self.context = None
        self.playwright = None
        self.pages = []
        self.active_idx = 0
        self.persistent = False
        self.profile_dir = None
        self.attached = False
        self.cdp = None
        self.console_logs.clear()
        self.network_events.clear()
        self._loop = None  # #002: session no longer bound to any loop

    async def close(self) -> None:
        async with self._get_lock():
            await self._close_locked()
        # Reset do singleton (#054): sem isto, proximo `BrowserSession.get()`
        # retorna a mesma instancia fechada com listeners stale. Reabrir cria
        # nova instancia limpa.
        BrowserSession._instance = None


def validate_browser_url(url: str) -> str | None:
    """Returns error string if URL is unsafe, None if OK."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "URL inválida"
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES:
        return f"Esquema '{scheme}' bloqueado por segurança"
    if scheme not in ("http", "https"):
        return f"Esquema '{scheme}' não permitido (use http ou https)"
    if not parsed.hostname:
        return "URL sem hostname"
    # userinfo (user:pass@host) e usado por phishing/SSRF para enganar o LLM:
    # `https://github.com:fake-token@evil.com` parece github mas resolve evil.
    if parsed.username or parsed.password:
        return "URL com userinfo (user:pass@) não permitida"
    try:
        from ..net_utils import validate_url as _validate

        return _validate(url)
    except Exception as e:
        # Fail-closed: previously returned None, which let metadata-service
        # IPs (169.254.169.254 / GCP) through whenever the SSRF validator
        # itself crashed. Truncate the URL in the log to avoid leaking
        # query-string secrets.
        logger.warning("SSRF validator failed for %s: %s", url[:80], e)
        return "Validação de URL indisponível — tente novamente"


def _atexit_say(msg: str) -> None:
    # See alpha/cli/lifecycle.py:_stderr for atexit-safe print rationale.
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


async def shutdown_browser() -> None:
    """Cleanup hook called on application shutdown.

    DR012/#015-V1.7: `browser.close()` pode travar em problemas de
    WebSocket/rede; sem o `wait_for`, atexit pendura o processo ate
    SIGKILL (em containers Docker, o timeout default de 10s).

    Etapa 2: a sessão normalmente vive no loop dedicado (_BrowserLoop) —
    o close precisa rodar LÁ (chamar de outro loop cruzaria objetos
    Playwright entre loops). Nota: o is_open() daqui NÃO serve de gate
    nesse caso — visto de outro loop ele reporta False por design (#002).
    """
    inst = BrowserSession._instance
    if inst is None:
        return
    try:
        if _BrowserLoop.alive() and inst._loop is _BrowserLoop._loop:
            fut = _BrowserLoop.submit(inst.close())
            try:
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=10)
            finally:
                _BrowserLoop.stop()
            return
        if not inst.is_open():
            return
        await asyncio.wait_for(inst.close(), timeout=10)
    except asyncio.TimeoutError:
        _atexit_say("shutdown_browser: timeout after 10s — forcing")
    except Exception as e:
        _atexit_say(f"shutdown_browser: {type(e).__name__}: {e}")
