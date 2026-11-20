"""
Conversation history persistence for Mythos.

Saves and loads conversation sessions to/from disk as JSON files.
Sessions are stored in ~/.alpha_code/history/ with timestamps.
Supports session summaries for quick resume and context continuity.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from ._json_utils import load_json_file
from .settings import atomic_write_text

# Session IDs come from `generate_session_id()` (timestamp_hex8) but the REPL
# also accepts user input via `/load <id>`. Without validation, an id like
# `../etc/passwd` resolves outside ~/.alpha_code/history/ — read or overwrite
# anywhere the process has access.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidSessionId(ValueError):
    """Raised when a session_id contains path-traversal characters."""

logger = logging.getLogger(__name__)

_HISTORY_DIR = Path.home() / ".alpha_code" / "history"
_MAX_SESSIONS = 50  # keep last N sessions on disk

# #DM075: caps do resumo de sessão (`_build_session_summary`) — por papel.
# Antes eram literais soltos (500/300/200/40) espalhados pela função.
_SUMMARY_LONG_CAP = 500    # [previous context] e conclusão do assistant
_SUMMARY_USER_CAP = 300    # pedido do usuário
_SUMMARY_TOOL_CAP = 200    # preview de tool result
_SUMMARY_MAX_PARTS = 40    # nº máximo de entradas mantidas (as últimas)


def _ensure_dir() -> Path:
    """Create history directory if needed.

    Perms 0o700: session files contem tool results (read_file de .env,
    output de execute_shell, query_database rows) e [CWD] do user. Em
    hosts compartilhados, deixar group/other-readable e leak.

    DEEP_SECURITY V3.0 #D113: o `_atomic_write` so chmoda arquivos quando
    sao escritos. Sessoes pre-fix #D109 (criadas com umask 022 → 0o644)
    permanecem world/group-readable indefinidamente. Defense-in-depth:
    a cada start, varremos os filhos *.json e elevamos perms para 0o600.
    Idempotente, barato (max ~50 arquivos por _MAX_SESSIONS), so faz
    chmod quando ja diverge — evita stat-thrash em hosts saudaveis.
    """
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_HISTORY_DIR, 0o700)
    except OSError:
        pass
    # Migra sessoes legadas para 0o600. Erros sao silenciados — pode ser
    # arquivo de outro usuario (em hosts compartilhados), filesystem r/o,
    # ou inode estranho. Logamos em debug, nao queremos derrubar startup.
    for child in _HISTORY_DIR.glob("*.json"):
        try:
            cur = child.stat().st_mode & 0o777
            if cur != 0o600:
                os.chmod(child, 0o600)
        except OSError as exc:
            logger.debug("history: skip chmod legacy %s: %s", child.name, exc)
    return _HISTORY_DIR


def _atomic_write(path: Path, data: str) -> None:
    """Atomically write `path` with mode 0o600 via temp-file + rename.

    Substitui `path.write_text(...)` que herda umask (default 0o644),
    expondo o conteudo entre usuarios em hosts compartilhados.

    #D110: a versao anterior abria o alvo com `O_TRUNC` e fazia um unico
    `os.write` cujo retorno era ignorado — uma escrita parcial (FS lento,
    sinal) ou um crash no meio deixavam o arquivo de sessao truncado/ilegivel.
    Agora escrevemos num temp no MESMO diretorio (mesmo filesystem → rename
    atomico) com `os.fdopen(...).write()` (itera ate gravar tudo) e
    promovemos via `os.replace`. Um crash so pode deixar um `.tmp` orfao,
    nunca um alvo corrompido. `mkstemp` cria com 0o600 e nome unico, evitando
    seguir symlink no alvo (o rename substitui o symlink, nao o segue).
    """
    # Preserva a defesa anti-symlink do O_NOFOLLOW original (#D109): recusa
    # escrever se o alvo ja e um symlink (ataque apontando pra fora do dir).
    # O temp+rename por si so nao seguiria o symlink (o substituiria), mas
    # manter a recusa explicita preserva o controle de seguranca documentado.
    if os.path.islink(path):
        raise OSError(f"history: refusing to write through symlink: {path}")
    # #DM066: a mecânica temp+fsync+fchmod+replace foi promovida para
    # `settings.atomic_write_text` (modelada nesta função); aqui fica só
    # a recusa de symlink, que é contrato específico da history (#D109).
    atomic_write_text(path, data, fsync=True, mode=0o600)


def _session_path(session_id: str) -> Path:
    """Resolve session_id to an absolute path, refusing traversal.

    Two layers of defense:
    1. Allowlist regex on the id itself (no `/`, no `.`, no spaces).
    2. After resolving, verify the path stays under the history dir —
       catches any byte the regex missed (defense in depth).
    """
    if not session_id or not _SESSION_ID_RE.match(session_id):
        raise InvalidSessionId(
            f"Session id must match {_SESSION_ID_RE.pattern} — got {session_id!r}"
        )
    base = _ensure_dir().resolve()
    candidate = (base / f"{session_id}.json").resolve()
    if not candidate.is_relative_to(base):
        raise InvalidSessionId(
            f"Session id {session_id!r} escapes the history directory"
        )
    return candidate


def generate_session_id() -> str:
    """Generate a unique session ID: timestamp + random suffix.

    Sufixo de 8 hex evita colisao quando dois agents/REPLs (ou parent+sub-agent)
    sao iniciados no mesmo segundo — sem o sufixo, o segundo `save_session`
    sobrescreve o primeiro silenciosamente, perdendo auditoria/forense.
    """
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"


def _build_session_summary(messages: list[dict]) -> str:
    """
    Build a compact summary of the session for continuity.

    Extracts user requests, assistant conclusions, and key tool results
    into a condensed text that can seed a new session.
    """
    # AUDIT_V1.8 #004: mensagens com imagem tem `content` como list[dict]
    # (multimodal). Listas nao-vazias passam pelo `or ""`, e logo abaixo
    # `text.startswith(...)` levantava AttributeError. Sessoes com `/image`,
    # Ctrl+V ou attachments nao salvavam (atexit engole excecao). Usar
    # `extract_text` (de alpha.attachments) reduz block lists para o texto
    # concatenado, ignorando blocos de imagem.
    from .attachments import extract_text
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = extract_text(content)

        if role == "user":
            # Strip CWD prefix
            text = content
            if text.startswith("[CWD:"):
                text = text.split("\n", 1)[-1] if "\n" in text else text
            if text.startswith("[CONTEXT SUMMARY"):
                # Already a summary — include as-is
                parts.append(f"[previous context]: {text[:_SUMMARY_LONG_CAP]}")
            else:
                parts.append(f"[user]: {text[:_SUMMARY_USER_CAP]}")

        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_names = []
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    tc_names.append(fn.get("name", "?"))
                parts.append(f"[assistant called: {', '.join(tc_names)}]")
            if content:
                # Keep last assistant response more fully (it's the conclusion)
                parts.append(f"[assistant]: {content[:_SUMMARY_LONG_CAP]}")

        elif role == "tool":
            # Very brief tool result summary
            preview = content[:_SUMMARY_TOOL_CAP] if content else "(empty)"
            parts.append(f"[tool result]: {preview}")

    return "\n".join(parts[-_SUMMARY_MAX_PARTS:])  # keep last N entries max


def _sanitize_for_save(messages: list[dict]) -> list[dict]:
    """Remove tool_call/tool message tuples that ficaram orfas.

    Cenario tipico: Ctrl+C entre o `assistant` que emite `tool_calls` e a
    `tool` response correspondente. Sem sanitizacao, a sessao salva fica
    com `assistant.tool_calls` sem responses (ou `tool` orfa) e o proximo
    `/load` quebra com HTTP 400 porque a API valida o emparelhamento.

    Estrategia (#DL-003): a versao anterior rastreava `pending_ids` num unico
    set que era SOBRESCRITO a cada `assistant.tool_calls`, entao so o ULTIMO
    assistant era saneado — um assistant ANTERIOR com respostas parciais
    (ex: Ctrl+C duplo) deixava tool_calls orfas que quebravam o `/load`. Agora
    saneamos em duas passadas, cobrindo TODOS os assistants:
    - Pass 1: que `tool_call_id`s realmente tem uma `tool` response.
    - Pass 2: dropar qualquer `assistant.tool_calls` que tenha QUALQUER id sem
      response (a API exige uma response por id).
    - Pass 3: dropar `tool` messages cujo id nao mapeia a um assistant
      sobrevivente (orfa, ou response parcial de assistant dropado).
    """
    if not messages:
        return messages

    # Pass 1 — ids that have a tool response anywhere in the transcript.
    responded = {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    # Pass 2 — keep an assistant.tool_calls only if EVERY id was responded.
    kept: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tc_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
            if tc_ids - responded:  # at least one call has no response
                continue  # orphan assistant — drop it
        kept.append(m)

    # Pass 3 — drop tool responses with no surviving assistant (orphan tool,
    # or a partial response whose assistant was just dropped).
    valid_ids = {
        tc.get("id")
        for m in kept
        if m.get("role") == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"] if tc.get("id")
    }
    cleaned = [
        m for m in kept
        if not (m.get("role") == "tool" and m.get("tool_call_id") not in valid_ids)
    ]
    return cleaned


def save_session(
    session_id: str, messages: list[dict], metadata: dict | None = None
) -> Path:
    """
    Save conversation messages to disk with a summary for continuity.

    Args:
        session_id: Unique session identifier.
        messages: Full message list (system + user + assistant + tool).
        metadata: Optional metadata (provider, model, etc).

    Returns:
        Path to the saved file.
    """
    # Sanitize first: remove orphan tool_calls/tool tuples (Ctrl+C race).
    messages = _sanitize_for_save(messages)

    # Filter out system messages and truncate tool results for storage
    storable = []
    for msg in messages:
        entry = dict(msg)
        if entry.get("role") == "system":
            continue  # skip system prompt (loaded fresh each time)
        if entry.get("role") == "tool":
            content = entry.get("content", "")
            if isinstance(content, str) and len(content) > 2000:
                entry["content"] = content[:2000] + "\n... [truncated for storage]"
        storable.append(entry)

    # Build session summary for quick resume
    summary = _build_session_summary(storable)

    data = {
        "session_id": session_id,
        "timestamp": time.time(),
        "timestamp_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "message_count": len(storable),
        "summary": summary,
        "messages": storable,
    }
    if metadata:
        data["metadata"] = metadata

    try:
        path = _session_path(session_id)
    except InvalidSessionId as e:
        logger.warning(f"save_session refused invalid id: {e}")
        return Path("")
    try:
        # JSON compacto (#D025): sessions sao consumidas por programa, nao
        # por humanos. `indent=2` inflava arquivos em 30-50% e custava ~30%
        # mais CPU sem ganho real.
        _atomic_write(path, json.dumps(data, ensure_ascii=False))
    except OSError as e:
        # Disco cheio / permissao / NFS hiccup nao deve derrubar o REPL.
        # Loga e retorna o path mesmo sem persistir — o caller decide se
        # tenta /save de novo ou aceita perda da sessao (#014/#D009).
        logger.warning(f"save_session failed ({path}): {e}")
        return path
    logger.info(f"Session saved: {path}")

    # Cleanup probabilistico (#D025): rodar `_cleanup_old_sessions` em
    # toda chamada faz glob + sorted desnecessario. ~10% das chamadas
    # mantem o tamanho do diretorio bounded sem custo per-save.
    if secrets.randbelow(10) == 0:
        try:
            _cleanup_old_sessions()
        except OSError as e:
            logger.debug(f"_cleanup_old_sessions failed: {e}")
    return path


def _load_session_data(session_id: str, caller: str) -> dict | None:
    """Read the on-disk JSON for a session, returning None on any failure.

    Invalid ids log a warning instead of raising so the REPL doesn't crash
    on a bad ``/load`` input.
    """
    try:
        path = _session_path(session_id)
    except InvalidSessionId as e:
        logger.warning(f"{caller} refused invalid id: {e}")
        return None
    if not path.exists():
        return None
    data = load_json_file(path, default=None, logger=logger)
    # #D809: JSON com topo não-dict (array/string/número editado à mão) não
    # pode virar AttributeError nos `.get(...)` dos callers.
    if data is not None and not isinstance(data, dict):
        logger.warning(
            f"{caller}: session {session_id} has non-dict top-level JSON; ignoring"
        )
        return None
    return data


def load_session(session_id: str) -> list[dict] | None:
    """Load conversation messages from disk. Returns None if not found.

    AUDIT_V2.3 #303: mesma defesa do save — um arquivo legado (anterior ao
    sanitizador), editado à mão ou gerado externamente não pode derrubar o
    próximo turno com HTTP 400 por `assistant.tool_calls` órfãos.
    """
    data = _load_session_data(session_id, "load_session")
    if data is None:
        return None
    raw = data.get("messages", [])
    # #700(a): entrada não-dict (arquivo editado à mão) não pode explodir o
    # sanitizador nem os consumidores — descarta antes.
    messages = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    return _sanitize_for_save(messages)


def load_session_summary(session_id: str) -> str | None:
    """Load just the session summary for lightweight resume."""
    data = _load_session_data(session_id, "load_session_summary")
    return None if data is None else data.get("summary")


def get_last_session_id() -> str | None:
    """Get the most recent session ID, or None if no sessions exist."""
    # #D111: persistence must never crash the REPL — a read-only FS or a
    # non-creatable ~/.alpha_code/history makes _ensure_dir() raise OSError.
    # Degrade to "no sessions" like the rest of the module does.
    try:
        history_dir = _ensure_dir()
    except OSError as exc:
        logger.debug("history: get_last_session_id _ensure_dir failed: %s", exc)
        return None
    files = sorted(history_dir.glob("*.json"), reverse=True)
    if not files:
        return None
    return files[0].stem


def list_sessions(limit: int = 20) -> list[dict]:
    """
    List recent sessions sorted by date (newest first).

    Returns list of {session_id, timestamp_human, message_count, preview}.

    #D007: para sessoes grandes (~MB), ler o arquivo inteiro so para extrair
    metadata era desperdicio. Lemos apenas os primeiros 8KB — basta para os
    campos `session_id`, `timestamp_human`, `message_count`, `summary`, e a
    primeira mensagem user (preview). Files maiores que 8KB ainda parseam
    corretamente porque o JSON parser precisa do objeto completo; entao
    fallback para read completo se o partial parse falhar.
    """
    # #D111: degrade to an empty list if the history dir can't be created
    # (read-only FS / permission denied), consistent with the REPL-never-
    # crashes-on-persistence invariant.
    try:
        history_dir = _ensure_dir()
    except OSError as exc:
        logger.debug("history: list_sessions _ensure_dir failed: %s", exc)
        return []
    sessions = []
    _PARTIAL_BYTES = 8192

    for path in sorted(history_dir.glob("*.json"), reverse=True)[:limit]:
        try:
            # Tentar partial read primeiro
            try:
                with open(path, "rb") as f:
                    chunk = f.read(_PARTIAL_BYTES)
                if len(chunk) < _PARTIAL_BYTES:
                    # Arquivo pequeno — chunk e o JSON inteiro
                    data = json.loads(chunk.decode("utf-8", errors="replace"))
                else:
                    # Arquivo maior — chunk e JSON truncado, parse falha;
                    # cai no fallback abaixo.
                    raise json.JSONDecodeError("partial read", "", 0)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Fallback: read completo
                data = json.loads(path.read_text(encoding="utf-8"))

            # #D809: topo não-dict ou `messages` não-lista não podem derrubar
            # a listagem inteira — pula o arquivo ruim e segue.
            if not isinstance(data, dict):
                continue
            raw_msgs = data.get("messages", [])
            if not isinstance(raw_msgs, list):
                raw_msgs = []

            # Find first user message as preview
            preview = ""
            for msg in raw_msgs:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    # AUDIT_V1.8 #004: sessoes salvas com multimodal vem como
                    # list[dict] na chave content — extract_text reduz a string.
                    if isinstance(content, list):
                        from .attachments import extract_text
                        content = extract_text(content)
                    if not isinstance(content, str):
                        content = ""
                    # Strip CWD prefix
                    if content.startswith("[CWD:"):
                        content = content.split("\n", 1)[-1]
                    preview = content[:80]
                    break

            sessions.append({
                "session_id": data.get("session_id", path.stem),
                "timestamp_human": data.get("timestamp_human", ""),
                "message_count": data.get("message_count", 0),
                "preview": preview,
            })
        except (json.JSONDecodeError, KeyError, OSError, AttributeError, TypeError):
            continue

    return sessions


def _cleanup_old_sessions():
    """Remove oldest sessions beyond _MAX_SESSIONS."""
    history_dir = _ensure_dir()
    files = sorted(history_dir.glob("*.json"))
    if len(files) > _MAX_SESSIONS:
        for old_file in files[: len(files) - _MAX_SESSIONS]:
            old_file.unlink(missing_ok=True)
            logger.debug(f"Cleaned up old session: {old_file.name}")
