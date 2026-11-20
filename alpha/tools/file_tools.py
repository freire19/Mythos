"""File operation tools for ALPHA agent.

Exposes the agent's read/write/search vocabulary on the filesystem:

- `read_file`, `write_file`, `edit_file` — atomic content I/O
- `list_directory` — non-recursive listing with metadata
- `glob_files`, `search_files` — pattern + content discovery (ripgrep fast-path)
- `make_directory`, `move_path`, `copy_path`, `remove_path` — mutation primitives
- `file_info` — stat + heuristic file-type detection

Invariants enforced here:

- All path arguments go through `path_helpers._validate_path` (workspace
  containment + traversal guard); destructive mutations additionally route
  through `_validate_path_no_symlink` so a symlink can't pivot out of the
  workspace at rename/replace time.
- `_SKIP_DIRS` skips noise directories (`.git`, `node_modules`,
  `.venv`, `__pycache__`, *_cache dirs) in recursive scans — these
  blow up `rglob` traversal time on monorepos and rarely have content
  the agent should be looking at.
- Search uses ripgrep when available (fast-path), falls back to a Python
  walker; both honor `_SKIP_DIRS` (via `_RG_EXCLUDES`, derived from it —
  the Python walker additionally skips hidden dirs) and never escape the
  workspace.

What's NOT here (lives elsewhere):

- Path validation utilities → `alpha.tools.path_helpers`
- Workspace bounds → `alpha.tools.workspace`
- Composite project introspection (`project_overview`, `run_tests`) →
  `alpha.tools._composite_*`
- Shell-based file ops (`mv`, `cp` via subprocess) →
  `alpha.tools.shell_tools` (the agent should prefer the structured
  tools here over shelling out)."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import shutil
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from .path_helpers import (
    _validate_path,
    _validate_path_no_symlink,
)
from .workspace import AGENT_WORKSPACE, assert_within_workspace

logger = logging.getLogger(__name__)

# Directories skipped during glob/recursive scans to avoid noise in monorepos
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__",
                         ".mypy_cache", ".pytest_cache", ".ruff_cache",
                         "dist", "build"})


# #025/#071 (V1.1): ripgrep (`rg`) e ~10-50x mais rapido que o scan Python
# (`os.walk` + `read_text` + regex line-a-line) em projetos > 1000 arquivos.
# Detectamos no import — `shutil.which` lookup e barato e o resultado
# nao muda a cada call. Quando ausente, caimos no fallback Python.
_RIPGREP_BIN = shutil.which("rg")
# #DM070: derivado de `_SKIP_DIRS` (fonte única) — antes era uma tupla menor
# mantida à mão, e editar `_SKIP_DIRS` era silenciosamente ignorado pela
# busca, contradizendo o invariante documentado no docstring do módulo.
_RG_EXCLUDES = tuple(sorted(_SKIP_DIRS))

# Inline edits load the whole file into memory; refuse anything larger so we
# don't allocate runaway buffers (the previous unconditional 10MB read also
# silently truncated bigger files).
_EDIT_MAX_BYTES = 10_000_000


# ─── Safe Tools ───


def _read_file_sync(p: Path, offset: int, limit: int) -> tuple[list[str], int]:
    """Sync helper para `_read_file` — move o I/O bloqueante para `to_thread`.

    AUDIT_V1.8 #041: este corpo era inline em `_read_file` (async), bloqueando
    o event loop em read de arquivos grandes (5MB streaming = 30-100ms).
    Separado para wrap com `asyncio.to_thread` no caller — mantemos a logica
    intacta, apenas movendo de coroutine para thread.
    """
    # DEEP_PERFORMANCE #D027: streaming read para arquivos grandes.
    # read_text() + splitlines() materializa o arquivo inteiro em RAM
    # mesmo com offset/limit pequenos. Para arquivos >= 100KB, fazemos
    # streaming com open() — conta total de linhas e só guarda as do range.
    # Custo: O(n) CPU (contar \n), O(limit) RAM (só as linhas visíveis).
    # #011: open with O_NOFOLLOW. `_validate_path` returns the RESOLVED path
    # (symlinks collapsed) and rejects targets outside the workspace, so for a
    # legit read the final component is never a symlink and O_NOFOLLOW is a
    # no-op. It only fires (ELOOP → caught upstream) if a component was swapped
    # for a symlink between validation and read (the residual TOCTOU window).
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(p), flags)
    opened = False
    try:
        fsize = os.fstat(fd).st_size
        buffering = 65536 if fsize >= 100_000 else -1
        f = os.fdopen(fd, "r", errors="replace", buffering=buffering)
        opened = True  # the file object now owns (and will close) fd
        with f:
            if fsize < 100_000:
                lines = f.read().splitlines()
                selected = lines[offset : offset + limit]
                total_lines = len(lines)
            else:
                selected = []
                total_lines = 0
                for i, line in enumerate(f):
                    total_lines = i + 1
                    if offset <= i < offset + limit:
                        selected.append(line.rstrip("\n"))
    finally:
        if not opened:
            os.close(fd)
    return selected, total_lines


async def _read_file(path: str, offset: int = 0, limit: int = 500) -> dict:
    """Read file contents with optional line range."""
    try:
        p = _validate_path(path)
    except PermissionError as e:
        return {"error": str(e)}
    if not p.exists():
        return {
            "error": f"Arquivo não encontrado: {path}",
            "workspace": str(AGENT_WORKSPACE),
            "hint": "O nome pode estar em inglês. Use list_directory() para verificar nomes reais.",
        }
    if not p.is_file():
        return {"error": f"Não é um arquivo: {path}"}
    try:
        # AUDIT_V1.8 #041: I/O bloqueante (read_text / stream open) wrapper
        # em `to_thread` para nao serializar `delegate_parallel` de file
        # operations. `_search_with_python` ja seguia o padrao desde V1.6.
        selected, total_lines = await asyncio.to_thread(
            _read_file_sync, p, offset, limit
        )
        numbered = "\n".join(f"{i + offset + 1}: {line}" for i, line in enumerate(selected))
        result = {
            "path": str(p),
            "total_lines": total_lines,
            "offset": offset,
            "lines_returned": len(selected),
            "content": numbered,
        }
        # Sinal em prosa quando o range pedido nao bate com o arquivo. Sem
        # isso, o modelo que chuta `offset=250` num arquivo de 200 linhas
        # recebe content="" + total_lines=200 e tende a refazer a leitura as
        # cegas. A frase deixa o overshoot/truncamento obvio em uma rodada.
        last_read = offset + len(selected)
        # `offset > 0` evita ruido no caso normal de ler um arquivo vazio do
        # inicio (offset=0, total=0); mas um offset positivo alem do fim —
        # inclusive num arquivo vazio (total=0) — merece o aviso explicito.
        if offset >= total_lines and offset > 0:
            result["note"] = (
                f"offset {offset} esta alem do fim do arquivo "
                f"({total_lines} linhas no total) — nada foi retornado."
            )
        elif last_read < total_lines:
            result["note"] = (
                f"truncado: restam {total_lines - last_read} linha(s). "
                f"Use offset={last_read} para continuar."
            )
        return result
    except Exception as e:
        return {"error": str(e)}


async def _list_directory(path: str = ".") -> dict:
    """List directory contents."""
    try:
        p = _validate_path(path)
    except PermissionError as e:
        return {"error": str(e)}
    if not p.exists():
        # Suggest similar directories in parent
        parent = p.parent if p.parent.exists() else AGENT_WORKSPACE
        suggestions = [e.name for e in parent.iterdir() if e.is_dir()][:15]
        return {
            "error": f"Diretório não encontrado: {path}",
            "workspace": str(AGENT_WORKSPACE),
            "hint": "O nome pode estar em inglês (Documents, Downloads, etc). Use list_directory() para ver diretórios disponíveis.",
            "nearby_dirs": suggestions,
        }
    if not p.is_dir():
        return {"error": f"Não é um diretório: {path}"}
    try:
        return await asyncio.to_thread(_list_directory_sync, p)
    except PermissionError:
        return {"error": f"Permissão negada: {path}"}


def _list_directory_sync(p: Path) -> dict:
    listing = sorted(p.iterdir())
    entries = []
    for entry in listing[:300]:
        try:
            info = {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
            }
            if entry.is_file():
                info["size"] = entry.stat().st_size
            entries.append(info)
        except PermissionError:
            entries.append({"name": entry.name, "type": "unknown", "error": "permissão negada"})
    return {"path": str(p), "count": len(listing), "entries": entries}


MAX_REGEX_PATTERN_LENGTH = 500


# Quantifier characters that, applied to a group, make ReDoS-prone shapes.
_QUANTIFIER_CHARS = ("*", "+", "?", "}")


def _detect_redos_pattern(pattern: str) -> str | None:
    """Best-effort static check for catastrophic-backtracking shapes.

    Returns a short reason string if the pattern matches a known ReDoS
    shape, else ``None``. Catches the cases the old subprocess timeout
    actually rejected in practice:

    - Nested quantifier on a group: ``(...)+`` / ``(...)*`` followed by
      another quantifier (``(a+)+``, ``(.*)+``, ``(.+)*``).
    - Star inside star: ``(.*)*``, ``(a*)*``.
    - Alternation with overlapping branches under a quantifier:
      ``(a|a)+``, ``(\\w|\\w+)+`` — heuristic, not exhaustive.

    Not a full regex parser — by design. We only look for shapes that
    are almost always ReDoS in practice; the search engine itself
    enforces a hard length cap (`MAX_REGEX_PATTERN_LENGTH`) above this.
    """
    # 1) `(...)<q1><q2>` where both q1 and q2 are quantifiers (e.g. `)*+`)
    #    or `)+*`. Also `){n,m}*` etc.
    i = 0
    depth = 0
    last_close: int | None = None
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            last_close = i
        elif ch in _QUANTIFIER_CHARS and last_close is not None and i == last_close + 1:
            # The just-closed group is now quantified. Look at the next char.
            if i + 1 < len(pattern) and pattern[i + 1] in _QUANTIFIER_CHARS:
                return "quantifier-on-quantified-group"
            # Look inside the just-closed group for an unbounded quantifier
            # (`*`, `+`, `{n,}`). Scan back to the matching `(`.
            open_pos = _matching_open_paren(pattern, last_close)
            if open_pos is not None and _has_unbounded_quantifier(
                pattern[open_pos + 1: last_close]
            ):
                # `(.*)+`, `(a+)*`, `(\w+)+` etc. — outer quantifier is `+`/`*`?
                if ch in ("+", "*"):
                    return "nested unbounded quantifiers"
            last_close = None
        else:
            last_close = None
        i += 1

    # 2) Alternation with duplicate single-char branches under a quantifier:
    #    `(a|a)+`, `(x|x)*`. Cheap pattern: `\((.)\|\1\)[+*]`.
    if re.search(r"\((.)\|\1\)[+*]", pattern):
        return "duplicate-branch alternation under quantifier"

    return None


def _matching_open_paren(pattern: str, close_idx: int) -> int | None:
    """Return the index of the `(` matching `pattern[close_idx]`, or None."""
    depth = 0
    i = close_idx
    while i >= 0:
        ch = pattern[i]
        if i > 0 and pattern[i - 1] == "\\":
            i -= 2
            continue
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                return i
        i -= 1
    return None


def _has_unbounded_quantifier(s: str) -> bool:
    """True if ``s`` contains an unescaped `*`, `+`, or `{n,}` quantifier."""
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            i += 2
            continue
        if s[i] in ("*", "+"):
            return True
        if s[i] == "{":
            # `{n,}` (no upper bound) is unbounded; `{n,m}` is bounded.
            close = s.find("}", i)
            if close != -1 and "," in s[i:close] and not s[i:close].rstrip("}").rstrip().endswith(","):
                # `{n,m}` — bounded
                pass
            elif close != -1 and "," in s[i:close]:
                return True
        i += 1
    return False


async def _search_files(pattern: str, path: str = ".", max_results: int = 50) -> dict:
    """Search for a text pattern inside files (grep-like)."""
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        return {"error": f"Padrão regex muito longo (máx {MAX_REGEX_PATTERN_LENGTH} chars)"}

    try:
        p = _validate_path(path)
    except PermissionError as e:
        return {"error": str(e)}
    if not p.exists():
        return {"error": f"Caminho não encontrado: {path}"}

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Regex inválido: {e}"}

    # Static ReDoS heuristic — replaces the old `subprocess.run([sys.executable,
    # "-c", ...])` validator that cost 30-100ms per call. The subprocess
    # approach existed to get a portable timeout, but it shipped a Python
    # interpreter just to compile + try-match a regex. The heuristic below
    # rejects the well-known ReDoS shapes (nested quantifiers on groups)
    # which is what the timeout caught in practice — we now reject
    # statically in microseconds.
    redos_reason = _detect_redos_pattern(pattern)
    if redos_reason:
        return {
            "error": (
                "Regex muito complexo (possível backtracking exponencial — "
                f"{redos_reason}). Simplifique o padrão."
            )
        }

    # #025/#071: ripgrep quando disponivel — 10-50x mais rapido em projetos
    # grandes. Fallback para scan Python preserva semantica (case-insensitive,
    # mesmas exclusoes de dir, mesmo formato de output).
    if _RIPGREP_BIN is not None:
        try:
            results = await _search_with_ripgrep(pattern, p, max_results)
        except _RipgrepTimeout:
            # #D043: surface the timeout as a failure (not a misleading
            # "0 matches") so the agent knows the search was incomplete.
            return {
                "error": (
                    "Busca excedeu 30s (ripgrep) e foi abortada — resultado "
                    "incompleto. Refine o padrão ou reduza o escopo (path)."
                ),
                "timeout": True,
                "pattern": pattern,
                "path": str(p),
            }
    else:
        results = await asyncio.to_thread(
            _search_with_python, regex, p, max_results
        )

    return {"pattern": pattern, "path": str(p), "matches": len(results), "results": results}


class _RipgrepTimeout(Exception):
    """#D043: ripgrep exceeded its time budget — distinct from 'no matches'."""


async def _search_with_ripgrep(pattern: str, root: Path, max_results: int) -> list[dict]:
    """Run `rg --json` and parse match lines.

    A regex e a mesma string passada pelo usuario (re.compile ja validou
    sintaxe Python; ripgrep usa Rust regex que aceita um subset compativel
    para padroes comuns). Em caso de divergencia de syntax, ripgrep
    retorna codigo != 0 e caimos no fallback Python.
    """
    excludes: list[str] = []
    for d in _RG_EXCLUDES:
        excludes.extend(["--glob", f"!{d}/**"])

    cmd = [
        _RIPGREP_BIN,  # type: ignore[list-item]
        "--json",
        "--ignore-case",
        "--max-count", str(max_results),
        "--max-filesize", "1M",
        "--no-messages",
        *excludes,
        "--regexp", pattern,
        str(root),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        # #D043: sinalizar o timeout em vez de devolver `[]` — vazio era
        # indistinguivel de "sem matches" e o agente concluia um falso
        # negativo. Nao caimos no fallback Python (provavelmente tambem
        # estouraria o tempo); o caller transforma isto num payload de erro.
        raise _RipgrepTimeout()
    except asyncio.CancelledError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise

    if proc.returncode not in (0, 1):  # 0=match, 1=no match; 2=erro
        # Provavel divergencia de syntax regex — fallback Python.
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return []
        return await asyncio.to_thread(
            _search_with_python, regex, root, max_results
        )

    out: list[dict] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        path_text = data.get("path", {}).get("text") or ""
        line_no = data.get("line_number") or 0
        line_text = data.get("lines", {}).get("text") or ""
        out.append({
            "file": path_text,
            "line": line_no,
            "content": line_text.rstrip("\n").strip()[:200],
        })
        if len(out) >= max_results:
            break
    return out


def _search_with_python(regex: "re.Pattern[str]", root: Path, max_results: int) -> list[dict]:
    """Fallback puro-Python para `_search_files` quando ripgrep nao esta no PATH."""
    out: list[dict] = []
    for dirpath, _dirs, files in os.walk(str(root)):
        if len(out) >= max_results:
            break
        _dirs[:] = [
            d for d in _dirs
            if not d.startswith(".")
            and d not in _RG_EXCLUDES
        ]
        for fname in files:
            if len(out) >= max_results:
                break
            fpath = Path(dirpath) / fname
            try:
                if fpath.stat().st_size > 1_000_000:
                    continue
                # DEEP_PERFORMANCE #037: streaming read em vez de
                # read_text() + splitlines() que materializa o arquivo
                # inteiro (até 999KB) em RAM duas vezes (string + lista).
                with open(fpath, "r", errors="replace", buffering=65536) as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            out.append({
                                "file": str(fpath),
                                "line": i,
                                "content": line.strip()[:200],
                            })
                            if len(out) >= max_results:
                                return out
            except (PermissionError, OSError):
                continue
    return out


def _glob_match_parts(
    parts: tuple[str, ...], pat_parts: tuple[str, ...], is_dir: bool
) -> bool:
    """Match relative path components against glob pattern components.

    Mirrors `pathlib.Path.glob` semantics (py3.11/3.12): `**` matches zero
    or more directories; a bare `**` tail matches directories only.
    """
    if not pat_parts:
        return not parts
    head, rest = pat_parts[0], pat_parts[1:]
    if head == "**":
        if not rest:
            return is_dir
        return any(
            _glob_match_parts(parts[i:], rest, is_dir)
            for i in range(len(parts) + 1)
        )
    if not parts:
        return False
    if not fnmatch.fnmatchcase(parts[0], head):
        return False
    return _glob_match_parts(parts[1:], rest, is_dir)


def _glob_recursive_sync(
    p: Path, pat_parts: tuple[str, ...], dirs_only: bool
) -> tuple[list[dict], int]:
    """Recursive glob via `os.walk` com poda real de `_SKIP_DIRS`."""
    matches: list[dict] = []
    skipped_outside = 0
    if not p.is_dir():
        return matches, skipped_outside
    if _glob_match_parts((), pat_parts, True):
        matches.append({"path": str(p), "type": "dir"})
    for dirpath, dirnames, filenames in os.walk(str(p)):
        # Skip subdirectories de noise para nao ler 100K entries em monorepos
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_parts = Path(dirpath).relative_to(p).parts
        candidates = [(name, True) for name in dirnames]
        if not dirs_only:
            candidates += [
                (name, False) for name in filenames if name not in _SKIP_DIRS
            ]
        for name, is_dir in candidates:
            if not _glob_match_parts(rel_parts + (name,), pat_parts, is_dir):
                continue
            entry = Path(dirpath) / name
            # #D025: workspace dentro do glob — se p contem symlink que aponta
            # para fora, glob seguiria e listaria filenames externos. read_file
            # depois bloquearia, mas o filename ja vazou estrutura externa.
            target = entry.resolve() if entry.is_symlink() else entry
            if assert_within_workspace(target):
                skipped_outside += 1
                continue
            info = {"path": str(entry), "type": "dir" if is_dir else "file"}
            if not is_dir:
                try:
                    info["size"] = entry.stat().st_size
                except OSError:
                    pass
            matches.append(info)
            if len(matches) >= 200:
                return matches, skipped_outside
    return matches, skipped_outside


def _glob_flat_sync(p: Path, pattern: str) -> tuple[list[dict], int]:
    """Glob nao-recursivo — mesmo comportamento de `p.glob`, fora do loop."""
    # #027/#072: era `sorted(p.glob(pattern))` — materializava a lista
    # inteira antes de cortar a 200. Em monorepos com .git/node_modules
    # incluido, isso pode produzir 100K+ entradas. Itera o generator,
    # acumula 200, ordena no final.
    matches: list[dict] = []
    skipped_outside = 0
    for match in p.glob(pattern):
        # Skip subdirectories de noise para nao ler 100K entries em monorepos
        try:
            rel = match.relative_to(p)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
        except ValueError:
            pass
        # #D025: workspace dentro do glob — se p contem symlink que aponta
        # para fora, glob seguiria e listaria filenames externos. read_file
        # depois bloquearia, mas o filename ja vazou estrutura externa.
        if assert_within_workspace(match.resolve()):
            skipped_outside += 1
            continue
        info = {"path": str(match), "type": "dir" if match.is_dir() else "file"}
        if match.is_file():
            try:
                info["size"] = match.stat().st_size
            except OSError:
                pass
        matches.append(info)
        if len(matches) >= 200:
            break
    return matches, skipped_outside


async def _glob_files(pattern: str, path: str = ".") -> dict:
    """Find files matching a glob pattern."""
    try:
        p = _validate_path(path)
    except PermissionError as e:
        return {"error": str(e)}
    if not p.exists():
        return {"error": f"Caminho não encontrado: {path}"}

    try:
        if "**" in pattern:
            if os.path.isabs(pattern):
                raise ValueError(pattern)
            pat_parts = tuple(
                seg for seg in pattern.split("/") if seg not in ("", ".")
            )
            if not pat_parts or any(
                "**" in seg and seg != "**" for seg in pat_parts
            ):
                raise ValueError(pattern)
            matches, skipped_outside = await asyncio.to_thread(
                _glob_recursive_sync, p, pat_parts, pattern.endswith("/")
            )
        else:
            matches, skipped_outside = await asyncio.to_thread(
                _glob_flat_sync, p, pattern
            )
    except PermissionError as e:
        return {"error": f"Permissão negada ao acessar: {e}"}
    except (ValueError, NotImplementedError):
        return {
            "error": "Pattern inválido: deve ser relativo (não absoluto) e não pode ser vazio"
        }

    # Sort apenas o subset de 200 (ou menos), nao a coleta inteira.
    matches.sort(key=lambda info: info["path"])

    result = {
        "pattern": pattern,
        "base_path": str(p),
        "count": len(matches),
        "matches": matches,
    }
    if skipped_outside:
        result["skipped_outside_workspace"] = skipped_outside
    return result


# ─── Destructive Tools ───


# Combo de flags da escrita atômica: O_NOFOLLOW faz o open falhar (ELOOP)
# se o alvo virou symlink entre a validação e a escrita (janela TOCTOU).
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW


def _overwrite_validated(p: Path, original_path: str, text: str) -> int:
    """Cauda comum de `_write_file_sync`/`_edit_file_sync` (#DM070 sub-achado).

    Re-valida o workspace (fecha o TOCTOU pós-mkdir/pós-read) e sobrescreve
    `p` com `text` em UTF-8 via O_NOFOLLOW. Retorna os bytes escritos.
    Levanta PermissionError se o path resolvido saiu do workspace.
    """
    p_resolved = Path(original_path).expanduser().resolve()
    err = assert_within_workspace(p_resolved)
    if err:
        raise PermissionError(err)
    data = text.encode("utf-8")
    fd = os.open(str(p), _WRITE_FLAGS, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return len(data)


def _write_file_sync(p: Path, original_path: str, content: str) -> dict:
    """Sync helper para `_write_file` (AUDIT_V1.8 #041).

    Move snapshot + mkdir + os.open + write para thread pool. A validacao
    de workspace continua sendo refeita pos-mkdir para fechar o TOCTOU
    (mesmo invariante que o codigo inline anterior).
    """
    previous_content = ""
    existed = p.exists()
    if existed and p.is_file():
        try:
            previous_content = p.read_text(errors="replace")
        except Exception:
            previous_content = ""

    # Checkpoint ANTES da mutação (rede de proteção do /rewind; nunca levanta).
    from ..checkpoints import snapshot as _ckpt_snapshot
    _ckpt_snapshot(p, "write_file")

    p.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = _overwrite_validated(p, original_path, content)
    return {
        "path": str(p),
        "bytes_written": bytes_written,
        "_previous_content": previous_content,
        "_created": not existed,
    }


async def _write_file(path: str, content: str) -> dict:
    """Write content to a file (creates parent directories if needed).

    Uses O_NOFOLLOW to prevent symlink TOCTOU attacks: if the target path
    is a symlink at the moment of opening, the call fails with ELOOP.
    """
    try:
        p = _validate_path_no_symlink(path)
    except PermissionError as e:
        return {"error": str(e)}
    try:
        # AUDIT_V1.8 #041: write bloqueante (snapshot read + mkdir +
        # os.open/write) movido para thread pool. Sem isso, `delegate_parallel`
        # com 5 writes serializa o event loop em 100-500ms cada.
        return await asyncio.to_thread(_write_file_sync, p, path, content)
    except OSError as e:
        if e.errno == 40:  # ELOOP — path is a symlink
            return {"error": "Acesso negado: operação de escrita em symlinks não é permitida"}
        return {"error": f"Erro de I/O: {e}"}
    except (PermissionError, ValueError) as e:
        return {"error": f"Acesso negado: {e}"}
    except Exception as e:
        return {"error": str(e)}


def _edit_file_sync(
    p: Path,
    original_path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict:
    """Sync helper para `_edit_file` (AUDIT_V1.8 #041)."""
    if not old_text:
        return {"error": "old_text vazio não é permitido"}
    fd = os.open(str(p), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        if size > _EDIT_MAX_BYTES:
            return {
                "error": f"Arquivo > {_EDIT_MAX_BYTES // 1_000_000}MB; "
                f"edição inline não suportada para arquivos desse tamanho"
            }
        raw = os.read(fd, size) if size > 0 else b""
        original = raw.decode("utf-8", errors="replace")
    finally:
        os.close(fd)
    if old_text not in original:
        return {"error": "Texto não encontrado no arquivo. Verifique indentação e espaços."}
    count = original.count(old_text)
    if count > 1 and not replace_all:
        # Ambiguidade explícita > substituição silenciosa da 1ª ocorrência:
        # o modelo escolhe entre ampliar o contexto ou assumir todas.
        return {
            "error": (
                f"old_text aparece {count} vezes no arquivo. Inclua mais "
                "contexto para torná-lo único, ou passe replace_all=true "
                "para substituir todas as ocorrências."
            ),
            "occurrences_found": count,
        }
    updated = original.replace(old_text, new_text) if replace_all else original.replace(
        old_text, new_text, 1
    )

    from ..checkpoints import snapshot as _ckpt_snapshot
    _ckpt_snapshot(p, "edit_file")

    _overwrite_validated(p, original_path, updated)
    return {
        "path": str(p),
        "occurrences_found": count,
        "replaced": count if replace_all else 1,
    }


async def _edit_file(
    path: str, old_text: str, new_text: str, replace_all: bool = False
) -> dict:
    """Edit a file by replacing old_text with new_text."""
    try:
        p = _validate_path_no_symlink(path)
    except PermissionError as e:
        return {"error": str(e)}
    if not p.exists():
        return {"error": f"Arquivo não encontrado: {path}"}
    try:
        # AUDIT_V1.8 #041: read+replace+write bloqueantes em thread pool.
        result = await asyncio.to_thread(
            _edit_file_sync, p, path, old_text, new_text, replace_all
        )
        # Edit-not-found path retorna dict com error sem levantar — repassa.
        if "error" in result:
            return result
        return {
            "path": result["path"],
            "occurrences_found": result["occurrences_found"],
            "replaced": result["replaced"],
        }
    except (PermissionError, ValueError) as e:
        return {"error": f"Acesso negado: {e}"}
    except Exception as e:
        return {"error": str(e)}


# ─── Registration ───


register_tool(
    ToolDefinition(
        name="read_file",
        description="Read a file's contents. Returns numbered lines with optional offset/limit for large files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start line (0-based). Default: 0",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lines to read. Default: 500",
                    "default": 500,
                },
            },
            "required": ["path"],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.FILESYSTEM,
        executor=_read_file,
    )
)

register_tool(
    ToolDefinition(
        name="list_directory",
        description="List a directory's contents (files and subdirectories with sizes).",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path. Default: current directory",
                    "default": ".",
                },
            },
            "required": [],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.FILESYSTEM,
        executor=_list_directory,
    )
)

register_tool(
    ToolDefinition(
        name="search_files",
        description="Search for a text pattern (regex) inside files recursively (like grep).",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {
                    "type": "string",
                    "description": "Base directory for the search. Default: current directory",
                    "default": ".",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results. Default: 50",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.FILESYSTEM,
        executor=_search_files,
    )
)

register_tool(
    ToolDefinition(
        name="glob_files",
        description="Find files by glob pattern (e.g. '**/*.py', '*.json').",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')"},
                "path": {
                    "type": "string",
                    "description": "Base directory. Default: current directory",
                    "default": ".",
                },
            },
            "required": ["pattern"],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.FILESYSTEM,
        executor=_glob_files,
    )
)

register_tool(
    ToolDefinition(
        name="write_file",
        description="Create or overwrite a file with the given content. Creates parent directories automatically.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the file to create/overwrite",
                },
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.FILESYSTEM,
        executor=_write_file,
    )
)

register_tool(
    ToolDefinition(
        name="edit_file",
        description=(
            "Edit a file by replacing one string with another (search-and-replace). "
            "ALWAYS read the file before editing. If old_text matches more than "
            "once, the call fails unless replace_all=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of the file to edit"},
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace (include enough context to be unique)",
                },
                "new_text": {
                    "type": "string",
                    "description": "New text that will replace the old one",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence of old_text (default: false)",
                    "default": False,
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.FILESYSTEM,
        executor=_edit_file,
    )
)
