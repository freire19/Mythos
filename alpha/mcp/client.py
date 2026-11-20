"""Stdio JSON-RPC client for a single MCP server.

The client spawns a subprocess, performs the MCP `initialize` handshake,
discovers tools via `tools/list`, and exposes `call_tool()` for execution.

Implementation note: uses a sync `subprocess.Popen` + reader thread instead
of `asyncio.subprocess`. The CLI recreates an event loop per turn
(`asyncio.run` in main.py), which would orphan asyncio streams bound to a
prior loop. A sync Popen survives across loops cleanly; async callers reach
it via `asyncio.to_thread`.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from collections import deque

from ..tools.safe_env import get_safe_env

# DEEP_SECURITY V3.3 #D122: vars de ambiente que NUNCA devem vir de
# `extra_env` em mcp.json. Mesmo que o usuario controle o mcp.json
# (trust model "settings sao do usuario"), um repositorio clonado com
# `.alpha/mcp.json` malicioso poderia injetar:
#   - LD_PRELOAD / LD_LIBRARY_PATH / DYLD_INSERT_LIBRARIES: carrega .so
#     arbitrario no subprocess MCP (RCE)
#   - PATH: redireciona binarios chamados pelo MCP (RCE indireto)
#   - PYTHONPATH / PYTHONHOME / PYTHONSTARTUP: idem para servidores MCP
#     escritos em Python
#   - HOME: pode mover ~/.config de outros tools que o MCP server use
_MCP_ENV_BLOCKLIST = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
    "PATH", "PATHEXT",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
    "HOME", "USERPROFILE",
    # #D142: other glibc/loader injection vectors + per-runtime loaders, in
    # case the MCP server is glibc/Node/Ruby/Perl/Java.
    "GCONV_PATH", "NLSPATH", "LOCPATH", "RESOLV_HOST_CONF", "HOSTALIASES",
    "NODE_PATH", "NODE_OPTIONS",
    "RUBYLIB", "RUBYOPT",
    "PERL5LIB", "PERL5OPT", "PERLLIB",
    "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH",
})

# DEEP_SECURITY V3.3 #D121: cap em notifications buffered. Servidor MCP
# que emita notification stream indefinidamente (ou bug em outro server
# do mesmo client) acumulava ate OOM. 100 e o suficiente para qualquer
# protocolo legitimo + sliding window via deque(maxlen) sem custo extra.
_NOTIFICATIONS_CAP = 100

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_REQUEST_TIMEOUT = 30.0
TOOL_CALL_TIMEOUT = 120.0


class MCPError(RuntimeError):
    """MCP-level error (server returned an error response or transport died)."""


def _package_version() -> str:
    """Versão real do pacote para o clientInfo (#500 — estava fixa em
    "1.0.0" copiada nos dois clients, stale desde a 1.x)."""
    try:
        from importlib.metadata import version

        return version("mythos")  # Mythos: o pacote instalado chama-se `mythos`
    except Exception:
        return "0"


class _MCPProtocolMixin:
    """AUDIT_V2.3 #500: métodos de protocolo MCP compartilhados.

    `initialize`/`list_tools`/`call_tool` eram byte-idênticos entre o
    client stdio e o Streamable HTTP — qualquer correção de handshake
    aplicada num deles ficava esquecida no outro. O transporte fica nas
    subclasses (`_request`/`_notify` + lifecycle); aqui só o protocolo.
    """

    # Providenciados pelas subclasses.
    name: str
    tools: list[dict]

    _CLIENT_INFO = {"name": "alpha-code", "version": _package_version()}

    def _request(
        self, method: str, params: dict | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict | None:  # pragma: no cover — abstrato por convenção
        raise NotImplementedError

    def _notify(self, method: str, params: dict | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    # Server capabilities from `initialize` (resources/prompts/tools…). Set by
    # `initialize`; empty when the server did not declare any.
    capabilities: dict = {}

    def initialize(self) -> dict:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(self._CLIENT_INFO),
            },
        )
        self._notify("notifications/initialized")
        result = result or {}
        caps = result.get("capabilities")
        self.capabilities = dict(caps) if isinstance(caps, dict) else {}
        return result

    # ── Resources (parity wave 2026-11, item 9) ──

    def supports_resources(self) -> bool:
        return isinstance(self.capabilities.get("resources"), dict)

    def list_resources(self, cursor: str | None = None) -> dict:
        """`resources/list` → {"resources": [...], "nextCursor": str|None}."""
        params = {"cursor": cursor} if cursor else None
        result = self._request("resources/list", params) or {}
        return {
            "resources": result.get("resources", []) or [],
            "nextCursor": result.get("nextCursor"),
        }

    def read_resource(self, uri: str, timeout: float | None = None) -> dict:
        """`resources/read` → raw result ({"contents": [...]})."""
        return self._request(
            "resources/read", {"uri": uri},
            timeout=timeout if timeout is not None else TOOL_CALL_TIMEOUT,
        ) or {}

    def list_tools(self) -> list[dict]:
        result = self._request("tools/list") or {}
        self.tools = result.get("tools", []) or []
        return self.tools

    def call_tool(
        self, tool_name: str, arguments: dict, timeout: float | None = None
    ) -> dict:
        # AUDIT_V2.2 #011: let the caller pass a shorter timeout (derived from
        # the turn budget). The blocking _request runs in a non-cancellable
        # to_thread worker, so without a tight timeout the thread can outlive
        # the agent turn by up to TOOL_CALL_TIMEOUT (120s).
        return self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout if timeout is not None else TOOL_CALL_TIMEOUT,
        ) or {}


class MCPClient(_MCPProtocolMixin):
    """Connection to one MCP server over stdio."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.extra_env = dict(env or {})
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        # DEEP_SECURITY V3.3 #D121: deque(maxlen) faz eviction FIFO automatico,
        # mantendo as 100 notifications mais recentes sem crescimento ilimitado.
        self._notifications: deque[dict] = deque(maxlen=_NOTIFICATIONS_CAP)
        self._notifications_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        # AUDIT_V1.8 #035: per-request queues evitam busy-spin de
        # re-enfileiramento no _inbox global. Reader thread roteia
        # resposta direto pra queue do request correspondente.
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()

    # ── Lifecycle ──

    def start(self) -> None:
        base_env = get_safe_env()
        # DEEP_SECURITY V3.3 #D122: filtrar vars perigosas do extra_env
        # antes do merge. Se mcp.json tenta injetar LD_PRELOAD/PATH/etc,
        # logamos warning e descartamos a entrada (nao falhamos o start —
        # o resto do extra_env pode ser legitimo).
        filtered_extra: dict[str, str] = {}
        for k, v in self.extra_env.items():
            if k.upper() in _MCP_ENV_BLOCKLIST:
                logger.warning(
                    "MCP '%s': env var %r ignored (in security blocklist)",
                    self.name, k,
                )
                continue
            filtered_extra[k] = v
        merged_env = {**base_env, **filtered_extra}
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                text=True,
                # #301: um único byte não-UTF-8 no stdout do servidor não pode
                # matar a reader thread com UnicodeDecodeError.
                errors="replace",
                bufsize=1,  # line-buffered
            )
        except (FileNotFoundError, PermissionError) as e:
            raise MCPError(f"Cannot spawn MCP server '{self.name}': {e}") from e

        self._reader = threading.Thread(
            target=self._read_stdout, name=f"mcp-{self.name}-reader", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name=f"mcp-{self.name}-stderr", daemon=True
        )
        self._stderr_reader.start()

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception as e:
            # #DM043: stdin already broken — proceeding to terminate is more
            # important than the close error. Debug log preserves diagnose.
            logger.debug("MCP %s stdin close failed: %s", self.name, e)
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            except Exception as e:
                # #DM043: terminate failed (PermissionError, OSError) —
                # log to diagnose zombies but continue shutdown.
                logger.debug("MCP %s terminate failed: %s", self.name, e)
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=2)
        self.proc = None

    # ── Internals ──

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        # AUDIT_V2.3 #301: a iteração de leitura em si pode levantar (ex.:
        # UnicodeDecodeError num byte inválido — mitigado pelo
        # errors="replace" do Popen, mas qualquer OSError/ValueError segue
        # possível). Sem o try/finally, a thread morria SEM enfileirar o
        # sentinela de desconexão: requests em voo esperavam o timeout
        # inteiro e toda request futura virava um buraco negro de 30-120s.
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("MCP %s: non-JSON line discarded: %r", self.name, line[:200])
                    continue
                # AUDIT_V1.8 #035: roteia resposta direto pra queue do
                # request pendente. Notifications (sem id) vao direto
                # pra _notifications; orfaos caem no _inbox global.
                msg_id = msg.get("id")
                if msg_id is not None:
                    with self._pending_lock:
                        pq = self._pending.get(str(msg_id))
                    if pq is not None:
                        pq.put(msg)
                        continue
                    # #D325: resposta órfã (id sem request pendente) — descarta com
                    # log; o antigo `_inbox` (write-only, nunca drenado) crescia sem
                    # limite sob um servidor que floodasse ids desconhecidos (OOM).
                    logger.debug(
                        "MCP %s: orphan response id=%s discarded", self.name, msg_id
                    )
                    continue
                # Notification (no id) — buffer directly.
                with self._notifications_lock:
                    self._notifications.append(msg)
        except Exception as e:
            logger.warning("MCP %s reader thread died: %s", self.name, e)
        finally:
            # EOF OU morte da thread: SEMPRE sinaliza desconexão para os
            # requests pendentes (e futuros verão o proc morto em _send).
            with self._pending_lock:
                for pq in self._pending.values():
                    pq.put(None)

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                line = line.rstrip()
                if line:
                    logger.debug("MCP %s [stderr]: %s", self.name, line)
        except Exception as e:  # #301: perder logging de stderr não pode matar nada
            logger.debug("MCP %s stderr reader died: %s", self.name, e)

    def _send(self, message: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.closed:
            raise MCPError(f"MCP server '{self.name}' is not running")
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"MCP server '{self.name}' write failed: {e}") from e

    def _request(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict | None:
        # AUDIT_V1.2 #007: release lock after send — holding it during the
        # entire wait loop serialized all RPCs to the same server.
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            req: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                req["params"] = params
            self._send(req)

        # AUDIT_V1.8 #035: per-request queue em vez de inbox global.
        # Evita busy-spin de re-enfileiramento quando respostas chegam
        # fora de ordem ou sao orfas.
        pq: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[str(req_id)] = pq

        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(
                        f"MCP '{self.name}' timed out on '{method}' after {timeout}s"
                    )
                try:
                    msg = pq.get(timeout=remaining)
                except queue.Empty:
                    raise MCPError(
                        f"MCP '{self.name}' timed out on '{method}' after {timeout}s"
                    ) from None

                if msg is None:
                    raise MCPError(
                        f"MCP '{self.name}' closed connection during '{method}'"
                    )

                # Server-initiated notifications shouldn't arrive here
                # (reader routes them to _notifications directly), but
                # guard defensively.
                if msg.get("id") is None:
                    with self._notifications_lock:
                        self._notifications.append(msg)
                    continue

                # Should always be our response since reader routes by id,
                # but verify to be safe.
                if str(msg.get("id")) != str(req_id):
                    continue

                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(
                        f"MCP '{self.name}' returned error on '{method}': "
                        f"{err.get('code')} {err.get('message')}"
                    )
                return msg.get("result")
        finally:
            with self._pending_lock:
                self._pending.pop(str(req_id), None)

    def _notify(self, method: str, params: dict | None = None) -> None:
        with self._lock:
            msg: dict = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            self._send(msg)
