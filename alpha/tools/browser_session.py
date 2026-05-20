"""
Persistent browser session for Alpha Code.

Holds a single Playwright browser instance shared across all browser_* tools
so cookies, login state, and tab history survive between tool calls.
"""

import asyncio
import logging
import sys
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        async_playwright,
    )

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = BrowserContext = Page = Playwright = None  # type: ignore


_BLOCKED_SCHEMES = frozenset(
    {"file", "chrome", "chrome-extension", "about", "javascript", "data", "view-source"}
)


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
        return self.browser is not None and self.browser.is_connected()

    async def open(self, headless: bool = True) -> None:
        async with self._get_lock():
            if self.is_open():
                return
            if not PLAYWRIGHT_AVAILABLE:
                raise RuntimeError(
                    "Playwright not installed. Run: "
                    "pip install playwright && playwright install chromium"
                )
            # Constroi tudo em locais antes de atribuir a self — se launch()
            # ou new_context() falhar, paramos o playwright e a instancia
            # fica num estado limpo. Sem isto, falhas de launch acumulam
            # 1 runtime por tentativa (combina com #054 close-leak).
            pw = await async_playwright().start()
            try:
                browser = await pw.chromium.launch(headless=headless)
                context = await browser.new_context(
                    user_agent="ALPHA-Browser/1.0",
                    viewport={"width": 1280, "height": 800},
                    accept_downloads=False,
                    java_script_enabled=True,
                )
                page = await context.new_page()
            except Exception:
                try:
                    await pw.stop()
                except Exception as cleanup_err:
                    logger.warning(f"playwright stop failed during cleanup: {cleanup_err}")
                raise
            self.headless = headless
            self.playwright = pw
            self.browser = browser
            self.context = context
            self.pages = [page]
            self.active_idx = 0
            self.context.on("page", self._on_new_page)
            self._wire_navigation_guard(page)

    def _on_new_page(self, page) -> None:
        if page not in self.pages:
            self.pages.append(page)
            self._wire_navigation_guard(page)

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
                    import asyncio as _aio
                    try:
                        loop = _aio.get_event_loop()
                        loop.create_task(page.goto("about:blank"))
                    except Exception as e:
                        logger.warning("Failed to rescue page from bad URL: %s", e)
            except Exception as e:
                logger.debug("framenavigated guard error: %s", e)

        try:
            page.on("framenavigated", _on_frame_nav)
        except Exception as e:
            logger.warning("Could not wire framenavigated listener: %s", e)

    async def close(self) -> None:
        async with self._get_lock():
            # #065: remover listener `_on_new_page` antes de fechar o
            # context. Sem isto, mesmo apos browser.close, o callback
            # mantinha referencia para `self` enquanto Playwright runtime
            # nao GC'ava o context — ciclos abre/fecha empilhavam listeners.
            if self.context is not None:
                try:
                    self.context.remove_listener("page", self._on_new_page)
                except Exception as e:
                    # Playwright pode levantar se o context ja foi descartado
                    # — nao impede o close, mas registramos para diagnostico
                    # em cenarios de stress (DR013).
                    logger.debug("browser close: remove_listener failed (non-fatal): %s", e)
            if self.browser:
                try:
                    await self.browser.close()
                except Exception as e:
                    logger.warning(f"Error closing browser: {e}")
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
    """
    if BrowserSession._instance is None or not BrowserSession._instance.is_open():
        return
    try:
        await asyncio.wait_for(BrowserSession._instance.close(), timeout=10)
    except asyncio.TimeoutError:
        _atexit_say("shutdown_browser: timeout after 10s — forcing")
    except Exception as e:
        _atexit_say(f"shutdown_browser: {type(e).__name__}: {e}")
