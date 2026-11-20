"""Resume of BACKGROUND sub-agents (parity wave 2026-11, follow-up).

`delegate_task(background=true)` spawns a detached headless Alpha
(`alpha -p`) through `bg_tasks`. That child exits when its task is done,
so — unlike an in-process sub-agent kept alive in `agent_sessions` —
there is no live object to send a follow-up message to.

Bridge (file-backed, same workspace):

1. The parent assigns the `agent_id` up front and hands the child
   `ALPHA_BG_TRANSCRIPT=<path>` (+ `ALPHA_BG_AGENT_ID`).
2. The child (`cli.entry.run_print`) dumps its final conversation to that
   path when it finishes — sanitized like a saved session (system prompt
   dropped, orphan tool_calls removed, tool outputs clipped).
3. `agent_message(agent_id, …)` in the parent, finding no in-memory
   session, loads the transcript, rebuilds an in-process sub-agent `prep`
   with the SAME id/scratch dir and that history, registers it in
   `agent_sessions` and continues the conversation in-process.

Trust model: the transcript is produced by a child *we* spawned, inside the
workspace `.alpha/` tree, but it is still data that an LLM wrote. Loading
validates the shape (roles, pairing) and clips sizes; the resumed
sub-agent runs under the parent's normal tool filters/blocklist, exactly
like any `agent_message` resume. Nothing here widens what a sub-agent can
do.

Layout: `<workspace>/.alpha/runs/bg/agents/<agent_id>.json`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TRANSCRIPT_ENV = "ALPHA_BG_TRANSCRIPT"
AGENT_ID_ENV = "ALPHA_BG_AGENT_ID"
INBOX_ENV = "ALPHA_BG_INBOX"

# Inbox: messages for a background child that is STILL RUNNING. The parent
# appends JSON lines to `<id>.inbox.jsonl`; the child drains it at the top
# of every iteration and again when the model is about to stop (so a
# message that arrives late still gets handled instead of being lost).
INBOX_MESSAGE_CAP = 8000
INBOX_MAX_PENDING = 20

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ROLES = frozenset({"user", "assistant", "tool"})

# Caps mirror `history.save_session` (2 000 chars per tool output) plus a
# message-count cap so a runaway child can't hand the parent a huge file.
TOOL_CONTENT_CAP = 2000
MAX_MESSAGES = 400
MAX_FILE_BYTES = 2_000_000
_PREVIEW = 200


def _valid_id(agent_id: str) -> bool:
    return bool(agent_id) and _AGENT_ID_RE.match(str(agent_id)) is not None


def transcripts_dir(workspace_root: str | os.PathLike) -> Path:
    return Path(workspace_root) / ".alpha" / "runs" / "bg" / "agents"


def transcript_path(workspace_root: str | os.PathLike, agent_id: str) -> Path:
    """Path of the transcript file for `agent_id`; raises on an unsafe id
    (no separators, no traversal — the id is a path component)."""
    if not _valid_id(agent_id):
        raise ValueError(f"agent_id inválido: {agent_id!r}")
    return transcripts_dir(workspace_root) / f"{agent_id}.json"


# ── child side ───────────────────────────────────────────────────────


def _storable(messages: list[dict]) -> list[dict]:
    from .history import _sanitize_for_save

    out: list[dict] = []
    for msg in _sanitize_for_save(list(messages or [])):
        if not isinstance(msg, dict) or msg.get("role") not in _ROLES:
            continue
        entry = dict(msg)
        if entry.get("role") == "tool":
            content = entry.get("content", "")
            if isinstance(content, str) and len(content) > TOOL_CONTENT_CAP:
                entry["content"] = content[:TOOL_CONTENT_CAP] + "\n... [truncated for storage]"
        # Provider-specific reasoning blocks are not portable across
        # providers/sessions; the resumed run may use another model.
        entry.pop("reasoning_content", None)
        entry.pop("thinking_blocks", None)
        out.append(entry)
    if len(out) > MAX_MESSAGES:
        out = _trim_keeping_pairs(out, MAX_MESSAGES)
    return out


def _trim_keeping_pairs(msgs: list[dict], keep: int) -> list[dict]:
    """Keep the LAST `keep` messages without splitting an assistant
    tool_calls entry from its tool results."""
    start = len(msgs) - keep
    while start < len(msgs) and msgs[start].get("role") == "tool":
        start += 1
    return msgs[start:]


def save_transcript(
    path: str | os.PathLike,
    messages: list[dict],
    *,
    task: str,
    status: str,
    reply: str,
    agent: str = "",
    provider: str = "",
) -> bool:
    """Write the child's final conversation. Never raises (the child's own
    output is what matters; the transcript is a convenience)."""
    from .settings import atomic_write_text

    p = Path(path)
    data = {
        "version": 1,
        "agent_id": p.stem,
        "saved_at": time.time(),
        "task": (task or "")[:_PREVIEW * 4],
        "status": status or "completed",
        "reply_preview": (reply or "")[:_PREVIEW],
        "agent": agent or "",
        "provider": provider or "",
        "messages": _storable(messages),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(data, ensure_ascii=False), mode=0o600)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning("bg transcript not saved (%s): %s", p, e)
        return False


def child_target() -> Path | None:
    """Where THIS process should dump its transcript, if it is a background
    sub-agent child (set by the parent in the environment)."""
    raw = os.environ.get(TRANSCRIPT_ENV, "").strip()
    return Path(raw) if raw else None


# ── parent side ──────────────────────────────────────────────────────


def _validate_messages(raw) -> list[dict] | None:
    if not isinstance(raw, list) or not raw:
        return None
    msgs: list[dict] = []
    pending_ids: set[str] = set()
    for m in raw:
        if not isinstance(m, dict) or m.get("role") not in _ROLES:
            return None
        role = m["role"]
        if role == "tool":
            tid = m.get("tool_call_id")
            if tid not in pending_ids:
                return None  # orphan tool result
            pending_ids.discard(tid)
        else:
            if pending_ids:
                return None  # assistant tool_calls without all results
            if role == "assistant":
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        pending_ids.add(tc["id"])
        msgs.append(m)
    if pending_ids:
        return None
    return msgs


def load_transcript(workspace_root: str | os.PathLike, agent_id: str) -> dict | None:
    """Load + validate a finished background sub-agent's transcript.
    Returns `None` when there is none (or it is malformed)."""
    if not _valid_id(agent_id):
        return None
    p = transcript_path(workspace_root, agent_id)
    try:
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("bg transcript unreadable (%s): %s", p, e)
        return None
    if not isinstance(data, dict) or data.get("agent_id") != agent_id:
        return None
    msgs = _validate_messages(data.get("messages"))
    if msgs is None:
        logger.warning("bg transcript %s rejected: malformed messages", p)
        return None
    return {
        "agent_id": agent_id,
        "messages": msgs,
        "task": str(data.get("task") or ""),
        "status": str(data.get("status") or "completed"),
        "reply_preview": str(data.get("reply_preview") or ""),
        "agent": str(data.get("agent") or ""),
        "provider": str(data.get("provider") or ""),
        "saved_at": float(data.get("saved_at") or 0.0),
        "path": str(p),
    }


def list_transcripts(workspace_root: str | os.PathLike, limit: int = 20) -> list[dict]:
    """Metadata of finished background sub-agents (newest first), for
    `agent_list`. Cheap: reads only small files, skips unreadable ones."""
    d = transcripts_dir(workspace_root)
    if not d.is_dir():
        return []
    try:
        files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out: list[dict] = []
    for f in files[: max(1, limit)]:
        info = load_transcript(workspace_root, f.stem)
        if info is None:
            continue
        out.append({
            "agent_id": info["agent_id"],
            "label": "background",
            "task": info["task"][:120],
            "turns": 1,
            "last_status": info["status"],
            "last_reply": info["reply_preview"],
            "messages": len(info["messages"]),
            "busy": False,
            "age_s": round(max(0.0, time.time() - info["saved_at"]), 1),
            "source": "background",
        })
    return out


def forget(workspace_root: str | os.PathLike, agent_id: str) -> bool:
    if not _valid_id(agent_id):
        return False
    try:
        transcript_path(workspace_root, agent_id).unlink()
        return True
    except OSError:
        return False


# ── inbox: message a RUNNING background child ────────────────────────


def inbox_path(workspace_root: str | os.PathLike, agent_id: str) -> Path:
    if not _valid_id(agent_id):
        raise ValueError(f"agent_id inválido: {agent_id!r}")
    return transcripts_dir(workspace_root) / f"{agent_id}.inbox.jsonl"


def post_message(workspace_root: str | os.PathLike, agent_id: str, text: str) -> Path:
    """Append one message for the running child. Control/bidi chars are
    stripped and the text capped; one `write` of one line keeps concurrent
    appends from interleaving."""
    from ._text_safety import strip_control_and_bidi

    p = inbox_path(workspace_root, agent_id)
    clean = strip_control_and_bidi(str(text))[:INBOX_MESSAGE_CAP]
    line = json.dumps({"ts": time.time(), "text": clean}, ensure_ascii=False) + "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return p


def pending_count(workspace_root: str | os.PathLike, agent_id: str) -> int:
    try:
        p = inbox_path(workspace_root, agent_id)
        if not p.is_file():
            return 0
        with p.open("rb") as fh:
            return sum(1 for ln in fh if ln.strip())
    except (OSError, ValueError):
        return 0


def drain_inbox(path: str | os.PathLike) -> list[str]:
    """Take every pending message, atomically: the inbox is renamed first
    (a concurrent append after the rename lands in a fresh inbox file and
    is picked up by the next drain), then read and removed. Never raises."""
    p = Path(path)
    if not p.is_file():
        return []
    taken = p.with_name(f"{p.name}.{os.getpid()}.{time.time_ns()}.read")
    try:
        os.replace(p, taken)
    except OSError:
        return []
    out: list[str] = []
    try:
        for raw in taken.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                text = obj.get("text") if isinstance(obj, dict) else None
            except ValueError:
                text = None
            if isinstance(text, str) and text.strip():
                out.append(text[:INBOX_MESSAGE_CAP])
    except OSError as e:
        logger.warning("bg inbox unreadable (%s): %s", taken, e)
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    return out[-INBOX_MAX_PENDING:]


def child_inbox() -> Path | None:
    """This process's inbox, if it is a background sub-agent child."""
    raw = os.environ.get(INBOX_ENV, "").strip()
    return Path(raw) if raw else None


def format_inbox_note(texts: list[str]) -> str:
    """User-role note injected into the child's history. The parent agent
    is the one that delegated the task, so its messages carry the same
    authority as the original task."""
    lines = [
        f"[ALPHA SYSTEM NOTE] {len(texts)} new message(s) from the parent agent "
        "that delegated this task. Treat them as additional instructions for "
        "the current task and keep working:"
    ]
    for i, t in enumerate(texts, 1):
        lines.append(f"--- message {i} ---\n{t}")
    return "\n".join(lines)
