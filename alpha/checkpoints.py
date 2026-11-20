"""File checkpoints — snapshot antes de cada mutação, `/rewind` restaura.

Gap nº1 vs Claude Code (Plano-Browser, seção "Gaps gerais"): uma sessão que
edita 15 arquivos e erra não tinha desfazer. Agora toda mutação de arquivo
(write_file, edit_file, search_and_replace) grava o conteúdo ANTERIOR em
`{workspace}/.alpha/checkpoints/` antes de escrever, e o REPL expõe:

    /rewind          — desfaz as edições do último turno (até o marcador)
    /rewind N        — desfaz as últimas N edições
    /rewind list     — mostra o histórico de checkpoints

Modelo de armazenamento (append-only, binário-seguro):

    .alpha/checkpoints/index.jsonl   — uma linha por evento:
        {"seq", "ts", "kind": "edit"|"marker", "tool", "path",
         "existed": bool, "blob": "42.blob"|null, "label": str|null}
    .alpha/checkpoints/blobs/<seq>.blob — bytes do arquivo ANTES da mutação
                                          (ausente quando o arquivo não existia)

Regras de segurança/robustez:
- `snapshot()` NUNCA levanta: qualquer falha vira log.warning e a escrita
  original prossegue (checkpoint é rede de proteção, não gate).
- `rewind()` re-valida cada path contra o workspace ativo antes de restaurar
  (mesmo guard das tools de arquivo) — um index adulterado não escreve fora.
- Kill-switch: ALPHA_CHECKPOINTS=0. Cap de entradas: ALPHA_CHECKPOINT_LIMIT
  (default 300) com poda dos blobs mais antigos.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .settings import atomic_write_text

try:
    import fcntl
except ImportError:  # Windows — sem flock; o lock por-processo segue valendo
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_INDEX_NAME = "index.jsonl"
_BLOBS_DIR = "blobs"
# #D307: `blob` vem do index.jsonl (potencialmente adulterado por conteúdo
# não-confiável). Só aceitar `<seq>.blob` (legado) ou `<seq>.<pid>.blob`
# (#204) — sem separador de path nem `..`.
_BLOB_NAME_RE = re.compile(r"^\d+(\.\d+)?\.blob$")
_DEFAULT_LIMIT = 300
# Arquivos maiores que isso não são snapshotados (blob gigante a cada save
# de um asset/binário grande). O rewind pula esses com aviso.
_MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024

_lock = threading.Lock()

# #DP046: contagem de linhas do index por diretório, mantida ao lado dos
# appends deste processo. Heurística POR-PROCESSO (outro processo pode
# appendar via flock #204 sem passar por aqui): serve só para pular o
# read+parse do index quando claramente abaixo do cap — na ausência da
# chave, ou ao cruzar o threshold, o disco continua sendo a verdade.
_count_cache: dict[str, int] = {}


@contextmanager
def _cross_process_lock(d: Path):
    """AUDIT_V2.3 #204: serializa parent × sub-agent background (processo
    `alpha -p` separado) escrevendo no MESMO `.alpha/checkpoints/`.

    O flock precisa cobrir a seção crítica INTEIRA (ler max do index →
    gravar blob → append) — sem ele, dois processos liam o mesmo max=N,
    ambos gravavam `N+1.blob` (um sobrescrevia o outro) e o `/rewind`
    restaurava o blob sobrevivente em AMBAS as entradas, perdendo o
    conteúdo anterior de um dos arquivos. Sem `fcntl` (Windows), degrada
    para o `threading.Lock` por-processo — a janela cross-process volta,
    mas o comportamento não piora em relação ao anterior."""
    if fcntl is None:
        yield
        return
    fd = os.open(str(d / ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def enabled() -> bool:
    # #DM061: default True (kill-switch) via vocabulário canônico — valor
    # não reconhecido continua caindo no default, como antes.
    from .config import env_bool

    return env_bool("ALPHA_CHECKPOINTS", default=True)


def _workspace() -> Path:
    from .tools.workspace import AGENT_WORKSPACE

    return Path(AGENT_WORKSPACE).resolve()


def _ckpt_dir(create: bool = False) -> Path:
    d = _workspace() / ".alpha" / "checkpoints"
    if create:
        (d / _BLOBS_DIR).mkdir(parents=True, exist_ok=True)
    return d


def _entry_limit() -> int:
    raw = os.environ.get("ALPHA_CHECKPOINT_LIMIT", "").strip()
    try:
        val = int(raw)
        if val > 0:
            return val
    except ValueError:
        pass
    return _DEFAULT_LIMIT


def _read_index(d: Path) -> list[dict]:
    idx = d / _INDEX_NAME
    if not idx.is_file():
        return []
    out: list[dict] = []
    try:
        for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # linha corrompida (crash no meio do append) — pula
            if isinstance(e, dict) and "seq" in e:
                out.append(e)
    except OSError as e:
        logger.warning("Checkpoint index unreadable: %s", e)
    return out


def _write_index(d: Path, entries: list[dict]) -> None:
    idx = d / _INDEX_NAME
    # #D318/#D808/#DM066: mkstemp nome único (não pode ser symlink pré-
    # plantado), fsync+utf-8 e replace atômico — tudo via o helper
    # compartilhado `settings.atomic_write_text`.
    atomic_write_text(
        idx,
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        fsync=True,
    )
    _count_cache.pop(str(d), None)


def _next_seq_locked(d: Path) -> int:
    """Próximo seq — SEMPRE relê o max do disco. Chamar com `_lock` +
    `_cross_process_lock` adquiridos (#204: o antigo cache por-processo
    era semeado uma única vez, então dois processos concorrentes alocavam
    o mesmo seq; o index é capado em ~300 linhas, reler é barato)."""
    entries = _read_index(d)
    return max((e["seq"] for e in entries), default=0) + 1


def _append(d: Path, entry: dict) -> None:
    idx = d / _INDEX_NAME
    # #D318: O_NOFOLLOW recusa (ELOOP) se idx for symlink — sem TOCTOU, sem
    # seguir um link pré-plantado (ex.: repo clonado que versiona index.jsonl
    # como symlink para ~/.gitconfig).
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(idx), flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    key = str(d)
    if key in _count_cache:
        _count_cache[key] += 1


def snapshot(path: str | Path, tool: str) -> None:
    """Grava o estado ANTERIOR de `path` antes de uma mutação. Nunca levanta."""
    if not enabled():
        return
    try:
        d = _ckpt_dir(create=True)
        p = Path(path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        with _lock, _cross_process_lock(d):
            existed = p.is_file()
            blob_name: str | None = None
            seq = _next_seq_locked(d)
            if existed:
                size = p.stat().st_size
                if size > _MAX_SNAPSHOT_BYTES:
                    logger.warning(
                        "Checkpoint skipped for %s — %d bytes > cap", p, size
                    )
                    return
                # #204: sufixo de pid — mesmo que dois processos aloquem o
                # mesmo seq numa janela residual, os blobs não se sobrescrevem.
                blob_name = f"{seq}.{os.getpid()}.blob"
                (d / _BLOBS_DIR / blob_name).write_bytes(p.read_bytes())
            _append(d, {
                "seq": seq,
                "ts": time.time(),
                "kind": "edit",
                "tool": tool,
                "path": str(p),
                "existed": existed,
                "blob": blob_name,
                "label": None,
            })
            _prune_locked(d)
    except Exception as e:  # rede de proteção não pode quebrar a escrita real
        logger.warning("Checkpoint snapshot failed for %s: %s", path, e)


def mark_turn(label: str) -> None:
    """Marca o início de um turno do usuário (fronteira do /rewind sem args).

    Marcadores consecutivos sem edição entre eles são colapsados (o último
    label vence) para o index não acumular ruído de turnos sem escrita.
    """
    if not enabled():
        return
    try:
        d = _ckpt_dir(create=True)
        with _lock, _cross_process_lock(d):
            entries = _read_index(d)
            label = (label or "").strip()[:80]
            if entries and entries[-1].get("kind") == "marker":
                entries[-1]["label"] = label
                entries[-1]["ts"] = time.time()
                _write_index(d, entries)
                return
            _append(d, {
                "seq": _next_seq_locked(d),
                "ts": time.time(),
                "kind": "marker",
                "tool": None,
                "path": None,
                "existed": False,
                "blob": None,
                "label": label,
            })
    except Exception as e:
        logger.warning("Checkpoint turn marker failed: %s", e)


def entries(limit: int = 30) -> list[dict]:
    """Últimas `limit` entradas do index (mais recente por último)."""
    try:
        with _lock:
            return _read_index(_ckpt_dir())[-limit:]
    except Exception:
        return []


def rewind(count: int | None = None) -> dict:
    """Restaura edições, da mais recente para trás.

    count=None → até o marcador de turno mais recente que preceda alguma
    edição (modo "/rewind": desfaz o último turno que escreveu algo).
    count=N    → desfaz as últimas N edições, ignorando marcadores.

    Entradas restauradas saem do index (um segundo /rewind volta mais).
    Retorna {"restored": [...], "skipped": [...], "message": str}.
    """
    from .tools.workspace import assert_within_workspace

    try:
        d = _ckpt_dir(create=True)
        with _lock, _cross_process_lock(d):
            all_entries = _read_index(d)
            if not any(e.get("kind") == "edit" for e in all_entries):
                return {"restored": [], "skipped": [],
                        "message": "Nenhum checkpoint de edição para restaurar."}

            # Seleciona o sufixo do index a desfazer.
            to_undo: list[dict] = []
            keep_until = len(all_entries)
            if count is not None:
                n = max(1, count)
                for i in range(len(all_entries) - 1, -1, -1):
                    if len(to_undo) >= n:
                        break
                    keep_until = i
                    if all_entries[i].get("kind") == "edit":
                        to_undo.append(all_entries[i])
            else:
                # Modo turno: pula marcadores do topo (turnos sem edição),
                # desfaz edições até encontrar o marcador que abre o turno.
                i = len(all_entries) - 1
                while i >= 0 and all_entries[i].get("kind") == "marker":
                    i -= 1
                keep_until = i + 1
                while i >= 0:
                    e = all_entries[i]
                    if e.get("kind") == "marker":
                        keep_until = i  # remove o marcador consumido também
                        break
                    to_undo.append(e)
                    keep_until = i
                    i -= 1

            if not to_undo:
                return {"restored": [], "skipped": [],
                        "message": "Nenhum checkpoint de edição para restaurar."}

            restored: list[str] = []
            skipped: list[dict] = []
            for e in to_undo:  # já está do mais recente pro mais antigo
                # #D307: resolver ANTES de validar containment — `relative_to`
                # é lexical e NÃO bloqueia `<ws>/../../etc/...`; sem o resolve()
                # um index adulterado escrevia arquivo arbitrário fora do ws.
                try:
                    p = Path(e.get("path") or "").expanduser().resolve()
                except (OSError, RuntimeError, ValueError):
                    skipped.append({"path": str(e.get("path")), "reason": "path inválido"})
                    continue
                err = assert_within_workspace(p)
                if err:
                    skipped.append({"path": str(p), "reason": err})
                    continue
                try:
                    if e.get("existed") and e.get("blob"):
                        blob_name = str(e["blob"])
                        # #D307: `blob` também é do index — recusar traversal.
                        if not _BLOB_NAME_RE.match(blob_name):
                            skipped.append({"path": str(p), "reason": "blob inválido"})
                            continue
                        blob = d / _BLOBS_DIR / blob_name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(blob.read_bytes())
                        restored.append(f"{p} ← restaurado ({e.get('tool')})")
                    else:
                        p.unlink(missing_ok=True)
                        restored.append(f"{p} ← removido (era criação nova)")
                except OSError as ex:
                    skipped.append({"path": str(p), "reason": str(ex)})

            # Remove do index tudo a partir de keep_until e apaga blobs órfãos.
            removed = all_entries[keep_until:]
            _write_index(d, all_entries[:keep_until])
            for e in removed:
                if e.get("blob"):
                    (d / _BLOBS_DIR / e["blob"]).unlink(missing_ok=True)

            msg = f"{len(restored)} arquivo(s) restaurado(s)."
            if skipped:
                msg += f" {len(skipped)} pulado(s)."
            return {"restored": restored, "skipped": skipped, "message": msg}
    except Exception as e:
        logger.warning("Rewind failed: %s", e)
        return {"restored": [], "skipped": [], "message": f"Rewind falhou: {e}"}


def _prune_locked(d: Path) -> None:
    """Poda o index ao cap (chamar com _lock já adquirido). Rewrite barato:
    só dispara quando excede o cap em 50 para amortizar o custo."""
    limit = _entry_limit()
    cached = _count_cache.get(str(d))
    if cached is not None and cached <= limit + 50:
        return
    entries_all = _read_index(d)
    _count_cache[str(d)] = len(entries_all)
    if len(entries_all) <= limit + 50:
        return
    dropped = entries_all[:-limit]
    _write_index(d, entries_all[-limit:])
    for e in dropped:
        if e.get("blob"):
            (d / _BLOBS_DIR / e["blob"]).unlink(missing_ok=True)
