"""Remote control — follow and drive the running REPL from a phone/browser.

`alpha --remote` (or `/remote on`) starts a small HTTP server next to the
REPL. The page it serves mirrors the conversation in real time and can:

- send a message (delivered through `repl_input.read_input`, exactly like
  typed input; queued while a turn is running),
- answer the approval menu and `ask_choice` (whichever side answers first
  wins — the terminal menu is released through its `cancel` event),
- interrupt the running turn.

Design constraints:

- **stdlib only** (`http.server` in a daemon thread). The REPL recreates
  its asyncio loop per turn, so the server cannot live on it.
- **Token auth.** Every `/api/*` call needs `Authorization: Bearer <token>`;
  the token travels to the page in the URL *fragment*, which browsers never
  send to the server or put in a Referer. The page itself is static.
- **LAN by default, plain HTTP.** Anyone holding the link can approve
  commands on this machine; anyone sniffing the network can read the
  token. Bind to loopback (`/remote on --local`) and put a TLS tunnel in
  front for use outside a trusted network.
- `!cmd` is refused from the remote: it runs a shell command with no
  approval gate, which is fine for the person at the keyboard and too much
  for a link.

Nothing here is imported at REPL startup unless the feature is used; every
public helper is a cheap no-op while the server is off.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import queue
import re
import secrets
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 7333
_BACKLOG_MAX = 400
_MAX_BODY = 64 * 1024
_MAX_MESSAGE_CHARS = 16_000
_MAX_CLIENTS = 8
_SUB_QUEUE_MAX = 2000
_PING_SECONDS = 15.0
_OUTPUT_CAP = 8000
_RESULT_LINES = 12
_RESULT_CHARS = 1200

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-B]|\x1b[=>78]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _plain(text: str) -> str:
    """Terminal output → text safe to show on the page."""
    text = _ANSI_RE.sub("", str(text)).replace("\r\n", "\n")
    # A bare CR redraws the line in a terminal; keep only the last version.
    text = "\n".join(ln.rsplit("\r", 1)[-1] for ln in text.split("\n"))
    return _CTRL_RE.sub("", text)


@dataclass
class Question:
    """A menu currently open in the terminal that the remote may answer."""

    id: str
    kind: str  # "approval" | "choice"
    title: str
    detail: str
    options: list[str]
    event: threading.Event = field(default_factory=threading.Event)
    answer: int | None = None  # 0-based index picked remotely
    cancelled: bool = False  # remote interrupt: treat as "no"/cancel

    def answered_remotely(self) -> bool:
        return self.answer is not None or self.cancelled


class _Hub:
    """Shared state between the REPL thread(s) and the HTTP threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._backlog: deque[dict] = deque(maxlen=_BACKLOG_MAX)
        self._subs: set[queue.Queue] = set()
        self._inbox: deque[str] = deque()
        self._question: Question | None = None
        self._busy = False
        self._waker = None
        self.info: dict = {}

    # ── events ──
    def publish(self, event: dict, persist: bool = True) -> None:
        with self._lock:
            if persist:
                self._seq += 1
                event = {**event, "seq": self._seq}
                self._backlog.append(event)
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subs.discard(q)
                # Wake the stalled client so its thread exits.
                with contextlib.suppress(Exception):
                    q.get_nowait()
                    q.put_nowait(None)

    def subscribe(self, since: int):
        """Register a live client. Returns (queue, hello, missed events) or
        None when the client cap is reached."""
        with self._lock:
            if len(self._subs) >= _MAX_CLIENTS:
                return None
            q: queue.Queue = queue.Queue(maxsize=_SUB_QUEUE_MAX)
            self._subs.add(q)
            missed = [e for e in self._backlog if e["seq"] > since]
            hello = {
                "type": "hello",
                "busy": self._busy,
                "info": dict(self.info),
                "question": _question_payload(self._question),
                "queued": list(self._inbox),
                "reset": bool(since and (not self._backlog or since > self._seq)),
            }
            return q, hello, missed

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def close_all(self) -> None:
        with self._lock:
            subs, self._subs = list(self._subs), set()
        for q in subs:
            with contextlib.suppress(Exception):
                q.put_nowait(None)

    def clients(self) -> int:
        with self._lock:
            return len(self._subs)

    # ── inbox (remote → REPL) ──
    def submit(self, text: str) -> None:
        with self._lock:
            self._inbox.append(text)
            waker = self._waker
        self.publish({"type": "queued", "text": text})
        if waker is not None:
            with contextlib.suppress(Exception):
                waker()

    def pop_message(self) -> str | None:
        with self._lock:
            return self._inbox.popleft() if self._inbox else None

    def clear_inbox(self) -> int:
        with self._lock:
            n = len(self._inbox)
            self._inbox.clear()
        if n:
            self.publish({"type": "queue_cleared"})
        return n

    def has_messages(self) -> bool:
        with self._lock:
            return bool(self._inbox)

    def set_waker(self, fn) -> None:
        with self._lock:
            self._waker = fn

    # ── busy / questions ──
    def set_busy(self, value: bool) -> None:
        with self._lock:
            changed = self._busy != value
            self._busy = value
        if changed:
            self.publish({"type": "status", "busy": value})

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    def open_question(self, q: Question) -> None:
        with self._lock:
            self._question = q
        self.publish({"type": "question", **_question_payload(q)})

    def close_question(self, q: Question, outcome: str) -> None:
        with self._lock:
            if self._question is q:
                self._question = None
        self.publish({
            "type": "question_closed",
            "id": q.id,
            "outcome": outcome,
            "source": "remote" if q.answered_remotely() else "terminal",
        })

    def answer(self, qid: str, index: int) -> bool:
        with self._lock:
            q = self._question
            if q is None or q.id != qid or q.event.is_set():
                return False
            if not 0 <= index < len(q.options):
                return False
            q.answer = index
            q.event.set()
            return True

    def interrupt(self) -> bool:
        """Stop the running turn. An open menu is cancelled first so its
        reader gives the terminal back before the SIGINT lands."""
        with self._lock:
            q = self._question
            busy = self._busy
            if q is not None and not q.event.is_set():
                q.cancelled = True
                q.event.set()
            else:
                q = None
        if not busy:
            return q is not None
        if q is not None:
            time.sleep(0.5)  # > select._CANCEL_POLL
        if self.is_busy():
            with contextlib.suppress(OSError):
                os.kill(os.getpid(), signal.SIGINT)
        return True


def _question_payload(q: Question | None) -> dict | None:
    if q is None:
        return None
    return {
        "id": q.id, "kind": q.kind, "title": q.title,
        "detail": q.detail, "options": list(q.options),
    }


# ── module state ──────────────────────────────────────────────────

_hub: _Hub | None = None
_server: ThreadingHTTPServer | None = None
_token: str = ""
_url: str = ""


def is_active() -> bool:
    return _hub is not None


def _lan_ip() -> str | None:
    """Address of the interface that routes to the local network. A UDP
    `connect` only picks a route — no packet leaves the machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def start(host: str | None = None, port: int | None = None) -> dict:
    """Start the server (idempotent). Returns `status()`.

    `host`: explicit bind address; default `ALPHA_REMOTE_HOST`, else the LAN
    address, else loopback. `port`: default `ALPHA_REMOTE_PORT` or 7333; if
    taken, the next nine are tried and then any free port.
    """
    global _hub, _server, _token, _url
    if _hub is not None:
        return status()

    host = host or os.getenv("ALPHA_REMOTE_HOST", "").strip() or _lan_ip() or "127.0.0.1"
    if port is None:
        try:
            port = int(os.getenv("ALPHA_REMOTE_PORT", "") or DEFAULT_PORT)
        except ValueError:
            port = DEFAULT_PORT

    server = None
    last_err: OSError | None = None
    for candidate in [port + i for i in range(10)] + [0]:
        try:
            server = _Server((host, candidate), _Handler)
            break
        except OSError as e:
            last_err = e
    if server is None:
        raise OSError(f"não foi possível abrir {host}:{port} — {last_err}")

    hub = _Hub()
    token = secrets.token_urlsafe(24)
    server.hub = hub  # type: ignore[attr-defined]
    server.token = token  # type: ignore[attr-defined]
    threading.Thread(
        target=server.serve_forever, name="alpha-remote", daemon=True
    ).start()

    _hub, _server, _token = hub, server, token
    shown_host = server.server_address[0]
    if ":" in shown_host:
        shown_host = f"[{shown_host}]"
    _url = f"http://{shown_host}:{server.server_address[1]}/#{token}"
    return status()


def stop() -> bool:
    global _hub, _server, _token, _url
    hub, server = _hub, _server
    if hub is None:
        return False
    _hub, _server, _token, _url = None, None, "", ""
    # Give any open menu back to the terminal only.
    hub.set_waker(None)
    hub.publish({"type": "bye"}, persist=False)
    hub.close_all()
    if server is not None:
        threading.Thread(target=_shutdown, args=(server,), daemon=True).start()
    return True


def _shutdown(server: ThreadingHTTPServer) -> None:
    with contextlib.suppress(Exception):
        server.shutdown()
        server.server_close()


def status() -> dict:
    if _hub is None or _server is None:
        return {"active": False}
    host, port = _server.server_address[:2]
    return {
        "active": True,
        "url": _url,
        "host": host,
        "port": port,
        "loopback": str(host).startswith("127.") or host == "::1",
        "clients": _hub.clients(),
    }


# ── REPL-side helpers (all no-ops while inactive) ─────────────────

def set_info(**info) -> None:
    hub = _hub
    if hub is not None:
        hub.info.update({k: v for k, v in info.items() if v is not None})


def publish(event: dict, persist: bool = True) -> None:
    hub = _hub
    if hub is not None:
        with contextlib.suppress(Exception):
            hub.publish(event, persist=persist)


def publish_user(text: str, source: str = "terminal") -> None:
    publish({"type": "user", "text": _plain(text)[:_MAX_MESSAGE_CHARS], "source": source})


def seed_history(history: list) -> None:
    """Show the conversation so far to a page opened mid-session."""
    hub = _hub
    if hub is None:
        return
    for entry in list(history)[-40:]:
        role, content = entry.get("role"), entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            hub.publish({"type": "user", "text": _plain(content)[:_MAX_MESSAGE_CHARS], "source": "history"})
        elif role == "assistant":
            hub.publish({"type": "assistant", "text": content})


def pop_message() -> str | None:
    hub = _hub
    return hub.pop_message() if hub is not None else None


def has_messages() -> bool:
    hub = _hub
    return hub.has_messages() if hub is not None else False


def clear_inbox() -> int:
    hub = _hub
    return hub.clear_inbox() if hub is not None else 0


def set_waker(fn) -> None:
    hub = _hub
    if hub is not None:
        hub.set_waker(fn)


def ask(kind: str, title: str, detail: str, options: list[str]) -> Question | None:
    """Announce a menu the remote may answer. Returns None while inactive.
    Pass `question.event` as the menu's `cancel` and call `close_question`
    when the menu is done, whoever answered."""
    hub = _hub
    if hub is None:
        return None
    q = Question(
        id=secrets.token_hex(6), kind=kind, title=_plain(title)[:500],
        detail=_plain(detail)[:6000],
        options=[_plain(o)[:400] for o in options],
    )
    hub.open_question(q)
    return q


def close_question(q: Question | None, outcome: str = "") -> None:
    if q is None:
        return
    hub = _hub
    if hub is not None:
        with contextlib.suppress(Exception):
            hub.close_question(q, _plain(outcome)[:300])


def describe_approval(tool_name: str, args: dict) -> tuple[str, str]:
    """(title, detail) for an approval card on the page: the tool and every
    argument, clipped — the same information the terminal card shows."""
    try:
        from .display.renderers.tools import _display_tool_name
        shown = _display_tool_name(tool_name)
    except Exception:
        shown = tool_name
    lines = []
    if isinstance(args, dict):
        for k, v in args.items():
            if isinstance(v, (dict, list)):
                try:
                    val = json.dumps(v, ensure_ascii=False, indent=1)
                except (TypeError, ValueError):
                    val = str(v)
            else:
                val = str(v)
            if len(val) > 2000:
                val = val[:2000] + f"… (+{len(val) - 2000} caracteres)"
            lines.append(f"{k}: {val}" if "\n" not in val else f"{k}:\n{val}")
    return f"{shown} ({tool_name})", "\n".join(lines)


def _result_text(name: str, result) -> tuple[str, bool]:
    """(short text, ok) for a tool result — a summary, never the payload."""
    if not isinstance(result, dict):
        return _plain(str(result))[:_RESULT_CHARS], True
    if result.get("error"):
        return "Error: " + _plain(str(result["error"]))[:_RESULT_CHARS], False
    if result.get("skipped"):
        return _plain(str(result.get("reason", "denied")))[:300], False
    try:
        from .display.renderers.tools import _bulk_summary, _result_summary_line
    except Exception:
        return "", result.get("ok") is not False
    summary = _bulk_summary(name, result)
    if summary:
        return summary, True
    if name in ("edit_file", "write_file"):
        verb = "Updated" if name == "edit_file" else "Wrote"
        return f"{verb} {result.get('path', '')}", True
    out = result.get("output") or result.get("content") or result.get("result") or result.get("stdout")
    if isinstance(out, str) and out.strip():
        lines = _plain(out).strip("\n").rstrip().split("\n")
        text = "\n".join(lines[:_RESULT_LINES])[:_RESULT_CHARS]
        if len(lines) > _RESULT_LINES:
            text += f"\n… +{len(lines) - _RESULT_LINES} linhas"
        return text, result.get("ok") is not False
    return _plain(_result_summary_line(result)), result.get("ok") is not False


_SELF_RENDERED = frozenset({"ask_choice"})


class TurnMirror:
    """Translates one turn's agent events into page events."""

    def __init__(self, hub: _Hub) -> None:
        self._hub = hub
        self._pending: list[str] = []
        self._any_text = False
        hub.set_busy(True)

    def _flush(self) -> None:
        if self._pending:
            text = "".join(self._pending)
            self._pending.clear()
            if text.strip():
                self._hub.publish({"type": "assistant", "text": text})

    def feed(self, event: dict) -> None:
        try:
            self._feed(event)
        except Exception:
            pass  # the mirror must never break the turn

    def _feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "token":
            text = event.get("text", "")
            if text:
                self._pending.append(text)
                self._any_text = True
                self._hub.publish({"type": "delta", "text": text}, persist=False)
        elif kind == "tool_call":
            self._flush()
            name = event.get("name", "")
            if name in _SELF_RENDERED:
                return
            args = event.get("args") or {}
            try:
                from .display.renderers.tools import _format_tool_call_header
                header = _plain(
                    _format_tool_call_header(name, args, event.get("safety", "safe"))
                ).lstrip("●⏺ ").strip()
            except Exception:
                header = name
            self._hub.publish({
                "type": "tool_call", "id": event.get("id") or name, "header": header,
            })
        elif kind == "tool_result":
            name = event.get("name", "")
            if name in _SELF_RENDERED:
                return
            text, ok = _result_text(name, event.get("result"))
            self._hub.publish({
                "type": "tool_result", "id": event.get("id") or name,
                "text": text, "ok": ok,
            })
        elif kind == "done":
            reply = event.get("reply", "")
            if reply and not self._any_text:
                self._pending[:] = [reply]
            self._flush()
        elif kind == "error":
            self._flush()
            self._hub.publish({
                "type": "error", "text": _plain(str(event.get("message", "erro")))[:2000],
            })
        elif kind == "anomaly":
            detail = _plain(str(event.get("detail", "")))[:500]
            if detail:
                self._hub.publish({"type": "note", "text": detail})
        elif kind == "context_compressed":
            self._hub.publish({"type": "note", "text": "contexto compactado"})

    def finish(self, aborted: bool = False, tools: int = 0, duration: float = 0.0) -> None:
        try:
            self._flush()
            self._hub.publish({
                "type": "turn_end", "aborted": bool(aborted),
                "tools": int(tools), "duration": round(float(duration), 1),
            })
        finally:
            self._hub.set_busy(False)


def turn_mirror() -> TurnMirror | None:
    hub = _hub
    return TurnMirror(hub) if hub is not None else None


class _Tee:
    """stdout stand-in that also keeps a copy of what is written."""

    def __init__(self, real) -> None:
        self._real = real
        self.parts: list[str] = []
        self._size = 0

    def write(self, data):
        if self._size < _OUTPUT_CAP * 4:
            self.parts.append(data)
            self._size += len(data)
        return self._real.write(data)

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def capture_output(command: str):
    """Mirror what a slash command prints. No-op while inactive."""
    hub = _hub
    # `/remote` prints the link (token included) — nothing the page needs.
    if hub is None or command.split(maxsplit=1)[0] == "/remote":
        yield
        return
    hub.publish({"type": "user", "text": _plain(command), "source": "command"})
    real = sys.stdout
    tee = _Tee(real)
    sys.stdout = tee
    try:
        yield
    finally:
        if sys.stdout is tee:
            sys.stdout = real
        text = _plain("".join(tee.parts)).strip("\n")
        if command.split(maxsplit=1)[0] == "/clear":
            publish({"type": "clear"})
        elif text.strip():
            if len(text) > _OUTPUT_CAP:
                text = text[:_OUTPUT_CAP] + "\n… (cortado)"
            publish({"type": "output", "text": text})


# ── terminal banner ───────────────────────────────────────────────

def qr_lines(url: str) -> list[str]:
    """The URL as a QR code drawn with half blocks, or [] when the optional
    `qrcode` package is missing (`pip install 'alpha-code[remote]'`)."""
    try:
        import qrcode  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(url)
        qr.make(fit=True)
        m = qr.get_matrix()
    except Exception:
        return []
    if len(m) % 2:
        m.append([False] * len(m[0]))
    # Dark modules drawn as background: scanners want dark-on-light, so the
    # light cells are the painted ones (█) and it works on dark terminals.
    glyph = {(False, False): "█", (True, True): " ", (False, True): "▀", (True, False): "▄"}
    return [
        "".join(glyph[(top, bottom)] for top, bottom in zip(m[i], m[i + 1]))
        for i in range(0, len(m), 2)
    ]


# ── HTTP ──────────────────────────────────────────────────────────

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
)
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
)


class _Handler(BaseHTTPRequestHandler):
    server_version = "AlphaRemote"
    sys_version = ""

    def log_message(self, *args) -> None:  # silence: stderr is the user's terminal
        pass

    # helpers
    def _send(self, code: int, body: bytes, ctype: str, extra=()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (*_SECURITY_HEADERS, *extra):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        given = header[7:].strip() if header.startswith("Bearer ") else ""
        expected = getattr(self.server, "token", "")
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 < length <= _MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # routes
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            from .remote_page import PAGE
            self._send(
                200, PAGE.encode(), "text/html; charset=utf-8",
                extra=(("Content-Security-Policy", _CSP),),
            )
            return
        if path == "/api/events":
            if not self._authorized():
                self._json(401, {"error": "token inválido"})
                return
            self._events()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "token inválido"})
            return
        hub: _Hub = self.server.hub  # type: ignore[attr-defined]
        if path == "/api/interrupt":
            self._json(200, {"ok": hub.interrupt()})
            return
        data = self._body()
        if data is None:
            self._json(400, {"error": "corpo inválido"})
            return
        if path == "/api/message":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                self._json(400, {"error": "mensagem vazia"})
                return
            text = _CTRL_RE.sub("", text.replace("\r\n", "\n")).strip()
            if len(text) > _MAX_MESSAGE_CHARS:
                self._json(400, {"error": "mensagem longa demais"})
                return
            if text.startswith("!") and not text.startswith("!["):
                self._json(400, {"error": "comandos ! só funcionam no terminal"})
                return
            hub.submit(text)
            self._json(200, {"ok": True})
            return
        if path == "/api/answer":
            qid, index = data.get("id"), data.get("index")
            if not isinstance(qid, str) or not isinstance(index, int) or isinstance(index, bool):
                self._json(400, {"error": "resposta inválida"})
                return
            self._json(200, {"ok": hub.answer(qid, index)})
            return
        self._json(404, {"error": "not found"})

    def _events(self) -> None:
        hub: _Hub = self.server.hub  # type: ignore[attr-defined]
        since = 0
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("since="):
                    with contextlib.suppress(ValueError):
                        since = max(0, int(part[6:]))
        sub = hub.subscribe(since)
        if sub is None:
            self._json(429, {"error": "clientes demais conectados"})
            return
        q, hello, missed = sub
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            for k, v in _SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            for event in (hello, *missed):
                self._emit(event)
            while True:
                try:
                    event = q.get(timeout=_PING_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self._emit(event)
        except (OSError, ValueError):
            pass  # client went away
        finally:
            hub.unsubscribe(q)

    def _emit(self, event: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()
