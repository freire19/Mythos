"""Shell pipeline execution tool for ALPHA agent.

Extends shell capabilities with pipe chains, redirects, and composed commands.
Each command in the pipeline is validated against the allowlist individually.

SECURITY: Each pipeline stage is validated. Hard-blocked patterns checked on full pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from .path_helpers import _validate_path_no_symlink
from .safe_env import get_safe_env
from ..config import TOOL_TIMEOUTS, TOOL_TIMEOUT_CAPS
from ._subprocess_helpers import STDERR_CAP, STDOUT_CAP
from ..security import validate_pipeline
from .workspace import AGENT_WORKSPACE, assert_within_workspace

logger = logging.getLogger(__name__)

# The only redirect target allowed outside the workspace: `2>/dev/null` is
# how every shell user silences noise, and the null device stores nothing.
# Matched as the exact literal — no normalization, so `/dev/null/../sda`
# or a symlink named like it never qualifies.
_DEV_NULL = "/dev/null"

def _validate_redirect_paths(pipeline: str) -> str | None:
    """Ensure redirect targets are within workspace.

    `2>&1` (e variantes `>&N`) sao FD duplications — `&1` nao e path nem
    cria arquivo. Pular esses para evitar tanto criar arquivos chamados
    `&1` quanto rejeitar pipelines comuns como `cmd 2>&1 | grep ...`.
    """
    # #010: tokenize quoting-aware (posix=False keeps quotes) so a `>` INSIDE a
    # quoted arg (`echo "a > b" > out`) isn't mistaken for a redirect operator.
    # The old raw regex captured `b"` as a spurious target → false-positive
    # workspace rejection. A standalone `>`/`2>>`/… token takes the NEXT token
    # as target; an attached form (`hi>file`, `>out`) takes its suffix.
    try:
        tokens = shlex.split(pipeline, posix=False)
    except ValueError:
        return None  # unbalanced quotes — downstream shlex will reject it
    _OP = re.compile(r"^(?:\d?>>?|&>>?)$")
    _ATTACHED = re.compile(r"(?:\d?>>?|&>>?)(\S+)$")
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and tok[0] in ("'", '"'):
            i += 1
            continue  # fully-quoted arg — never a redirect operator
        if _OP.match(tok):
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1])
        else:
            m = _ATTACHED.search(tok)
            if m:
                targets.append(m.group(1))
        i += 1
    for target in targets:
        if target.startswith("&"):
            continue  # &1, &2 sao FD references, nao paths
        clean = target.strip().strip("'\"")
        if not clean or clean == _DEV_NULL:
            continue  # discarding output writes nothing anywhere
        target_path = Path(clean).expanduser().resolve()
        err = assert_within_workspace(target_path)
        if err:
            return f"Redirect para '{target}': {err}"
    return None


_REDIRECT_RE = re.compile(r"(2>>|2>|>>|>|<)\s*(\S+)")

# #P001: split que PRESERVA o operador (capturing group) — distinto de
# PIPELINE_SPLIT_RE que descarta. Usado pelo executor de pipeline para
# decidir short-circuit (&&, ||) e separador (;).
_LOGICAL_SPLIT_KEEP_RE = re.compile(r"\s*(&&|\|\||;)\s*")


def _parse_segment(segment: str) -> tuple[list[str], dict[str, str]]:
    """Split command and redirects from a pipeline segment.

    Returns (cmd_parts, redirects_dict).  FD duplication (``2>&1``,
    ``1>&2`` etc.) is recognized and skipped — those are shell-level
    operations, not file paths.
    """
    redirects = {}
    for match in _REDIRECT_RE.finditer(segment):
        op, target = match.group(1), match.group(2)
        # FD duplication: 2>&1, 1>&2 — not a file path.
        if target.startswith("&"):
            continue
        # Strip shell quoting que o regex captura junto com o path.
        target = target.strip().strip("'\"")
        if op == ">":
            redirects["stdout"] = target
        elif op == ">>":
            redirects["stdout_append"] = target
        elif op == "2>":
            redirects["stderr"] = target
        elif op == "2>>":
            redirects["stderr_append"] = target
        elif op == "<":
            redirects["stdin"] = target

    for a, b in (("stdout", "stdout_append"), ("stderr", "stderr_append")):
        if a in redirects and b in redirects:
            raise ValueError(
                f"Redirects conflitantes no mesmo comando: "
                f"'{'>' if a == 'stdout' else '2>'}' e "
                f"'{'>>' if a == 'stdout' else '2>>'}'"
            )

    cmd_str = _REDIRECT_RE.sub("", segment).strip()
    parts = shlex.split(cmd_str) if cmd_str else []
    return parts, redirects


# AUDIT_V1.2 #003 + DEEP_SECURITY V3.0 #D117: O_NOFOLLOW previne TOCTOU
# symlink. Janela de ataque do bug original: entre `Path(t).resolve()`
# (que segue symlinks ate alvo, ok no workspace) e `open(target_path, "w")`
# (que abre por nome — atacante local pode trocar o path por symlink
# apontando pra fora do workspace nessa janela). Mesma fix aplicada em
# `file_tools._write_file/_edit_file` apos #017 V1.1.
_REDIRECT_OPEN_FLAGS = {
    "stdout":         os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    "stdout_append":  os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    "stderr":         os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    "stderr_append":  os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    "stdin":          os.O_RDONLY,
}
_REDIRECT_FDOPEN_MODE = {
    "stdout": "w", "stdout_append": "a",
    "stderr": "w", "stderr_append": "a",
    "stdin":  "r",
}


def _open_redirect_target(key: str, target_path: Path):
    """Abre um redirect target com O_NOFOLLOW.

    `O_NOFOLLOW` (Linux/macOS) faz o open() falhar com `OSError(ELOOP)` se
    o path e um symlink. Em Windows o flag nao existe — caimos no `open()`
    builtin (sem proteccao TOCTOU, mas Windows nao e plataforma alvo).
    """
    flags = _REDIRECT_OPEN_FLAGS[key]
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(target_path), flags, 0o644)
    return os.fdopen(fd, _REDIRECT_FDOPEN_MODE[key])


def _open_redirect_files(redirects: dict[str, str]) -> dict:
    """Abre file handles para redirects. Valida contra workspace.

    #D004/#008 (V1.1 RES): se a validacao de workspace falhar no meio do
    loop (e.g. `cmd > ok.txt 2> /etc/passwd` onde a primeira passa e a
    segunda nao), os handles ja abertos vazavam — `raise ValueError`
    deixava o dict orfa e o caller perdia a chance de fechar. Agora todo
    o open acontece num try/except que fecha handles ja abertos antes
    de re-raise, mantendo o invariante "ou retorna tudo aberto, ou nao
    deixa fd vazado".

    AUDIT_V1.2 #003 + DEEP_SECURITY V3.0 #D117: usa `os.open(O_NOFOLLOW)`
    em vez de `open()` para fechar TOCTOU symlink. Atacante que conseguir
    trocar o path por symlink entre `relative_to()` e o `open` recebe
    `OSError(ELOOP)` em vez de escrever no destino do symlink.
    """
    handles: dict = {}
    try:
        for key, target in redirects.items():
            if target == _DEV_NULL:
                handles[key] = open(os.devnull, _REDIRECT_FDOPEN_MODE[key])
                continue
            # Validacao de path: rejeita symlinks no raw input E em parents
            # ANTES do open. Mesmo helper usado por write_file/edit_file. Para
            # paths inexistentes (caso comum em redirect `> new.txt`) cai no
            # fallback manual de workspace check.
            raw = Path(target).expanduser()
            if not raw.is_absolute():
                raw = AGENT_WORKSPACE / raw
            try:
                if raw.exists():
                    target_path = _validate_path_no_symlink(str(raw))
                else:
                    # Path nao existe ainda: validar parents e logical containment.
                    # `os.path.normpath` colapsa `..` puramente textualmente
                    # (sem touch FS), evitando bypass `cmd > ../../etc/passwd`.
                    import os.path as _osp
                    norm = Path(_osp.normpath(str(raw)))
                    err = assert_within_workspace(norm)
                    if err:
                        raise PermissionError(
                            f"Redirect '{target}': {err}"
                        )
                    # Walk parents existentes — qualquer symlink no caminho
                    # ate a raiz seria explorado pelo open.
                    cur = norm.parent
                    while cur != cur.parent and not cur.exists():
                        cur = cur.parent
                    walk = cur
                    while walk != walk.parent:
                        if walk.is_symlink():
                            raise PermissionError(
                                f"Redirect '{target}' tem componente symlink "
                                f"({walk}) — bloqueado por seguranca"
                            )
                        walk = walk.parent
                    target_path = norm
            except PermissionError as exc:
                # PermissionError -> ValueError pra manter semantica do caller
                # (que faz `except (ValueError, ...)`).
                raise ValueError(str(exc)) from exc

            try:
                handles[_REDIRECT_KEY_TO_HANDLE[key]] = _open_redirect_target(
                    key, target_path
                )
            except OSError as exc:
                # ELOOP (errno 40 em Linux) = path era symlink criado entre
                # validacao e open (TOCTOU race). Erro de validacao, nao
                # runtime — mensagem explicita.
                if exc.errno == 40:
                    raise ValueError(
                        f"Redirect '{target}' e symlink — bloqueado por seguranca"
                    ) from exc
                raise
    except Exception:
        # Cleanup parcial: fecha tudo o que conseguimos abrir antes do erro.
        # Engole erros de close (handle ja invalido / disco quebrado) — o
        # erro original e o que o caller precisa ver.
        for fh in handles.values():
            try:
                fh.close()
            except Exception:
                pass
        raise
    return handles


# Mapeia o key parseado pra chave que o caller usa em subprocess_exec.
# stdout_append / stderr_append viram "stdout" / "stderr" porque o flag
# `O_APPEND` ja foi escolhido na hora do open.
_REDIRECT_KEY_TO_HANDLE = {
    "stdout": "stdout", "stdout_append": "stdout",
    "stderr": "stderr", "stderr_append": "stderr",
    "stdin":  "stdin",
}


async def _execute_pipe_chain(
    pipe_segments: list[str],
    cwd: str,
    env: dict,
    timeout: int,
) -> tuple[int, str, str]:
    """
    Executa uma cadeia de pipes (cmd1 | cmd2 | cmd3) sem shell.
    Cada segmento é executado com create_subprocess_exec.
    Para pipes, lê stdout do anterior e passa como stdin do próximo.
    Retorna (exit_code, stdout, stderr).
    """
    if not pipe_segments:
        return 0, "", ""

    # AUDIT_V1.10 #049: single-cmd e multi-cmd compartilhavam ~30 linhas
    # de lógica quase idêntica (create_subprocess_exec, communicate com
    # timeout, kill+wait, close handles). Unificado em loop único.
    prev_output = None
    all_stderr = ""
    last_exit_code = 0

    is_single = len(pipe_segments) == 1

    for seg in pipe_segments:
        parts, redirects = _parse_segment(seg)
        if not parts:
            continue

        handles = _open_redirect_files(redirects)
        try:
            # Single-cmd usa handles para stdin; multi-cmd pipeia stdout→stdin.
            stdin_src = (
                (handles.get("stdin") or asyncio.subprocess.DEVNULL)
                if (is_single and prev_output is None)
                else asyncio.subprocess.PIPE
            )
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdin=stdin_src,
                stdout=handles.get("stdout", asyncio.subprocess.PIPE),
                stderr=handles.get("stderr", asyncio.subprocess.PIPE),
                cwd=cwd,
                env=env,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=prev_output),
                    timeout=timeout,
                )
            except (TimeoutError, asyncio.CancelledError):
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    logger.exception(
                        f"Pipeline process {proc.pid} didn't die after kill"
                    )
                except Exception:
                    logger.exception(
                        f"Pipeline process {proc.pid}: unexpected error after kill"
                    )
                raise
            prev_output = stdout_bytes
            last_exit_code = proc.returncode
            if stderr_bytes:
                all_stderr += stderr_bytes.decode(errors="replace")
        finally:
            for h in handles.values():
                if hasattr(h, "close"):
                    h.close()

    return (
        last_exit_code,
        (prev_output or b"").decode(errors="replace"),
        all_stderr,
    )


async def _execute_pipeline(pipeline: str, cwd: str | None = None, timeout: int | None = None) -> dict:
    """Execute a shell pipeline with pipes, redirects, and operators — sem shell."""
    if timeout is None:
        timeout = TOOL_TIMEOUTS.get("pipeline", 240)

    # Validate entire pipeline
    block_reason = validate_pipeline(pipeline)
    if block_reason:
        return {"error": block_reason, "blocked": True}

    # Validate redirect paths
    redirect_error = _validate_redirect_paths(pipeline)
    if redirect_error:
        return {"error": redirect_error, "blocked": True}

    # Validate cwd
    if cwd:
        cwd_path = Path(cwd).expanduser().resolve()
        err = assert_within_workspace(cwd_path)
        if err:
            return {"error": err}
        cwd = str(cwd_path)
    else:
        cwd = str(AGENT_WORKSPACE)

    timeout = min(timeout, TOOL_TIMEOUT_CAPS.get("pipeline", 120))
    env = get_safe_env()

    try:
        # Separar por operadores lógicos (&&, ||, ;) preservando o operador
        logical_segments = _LOGICAL_SPLIT_KEEP_RE.split(pipeline)

        final_stdout = ""
        final_stderr = ""
        last_exit_code = 0

        i = 0
        skip_next = False
        while i < len(logical_segments):
            part = logical_segments[i].strip()
            i += 1

            if not part:
                continue

            # É um operador lógico?
            if part in ("&&", "||", ";"):
                if part == ";":
                    # Unconditional separator — always continue
                    skip_next = False
                elif (part == "&&" and last_exit_code != 0) or (part == "||" and last_exit_code == 0):
                    # Short-circuit: skip the NEXT command only
                    skip_next = True
                else:
                    skip_next = False
                continue

            if skip_next:
                skip_next = False
                continue

            # É um pipe chain: cmd1 | cmd2 | cmd3
            pipe_segments = [s.strip() for s in part.split("|") if s.strip()]

            exit_code, stdout, stderr = await _execute_pipe_chain(
                pipe_segments,
                cwd,
                env,
                timeout,
            )
            last_exit_code = exit_code
            final_stdout += stdout
            final_stderr += stderr

        return {
            "exit_code": last_exit_code,
            "stdout": final_stdout[:STDOUT_CAP],
            "stderr": final_stderr[:STDERR_CAP],
            "pipeline": pipeline,
        }
    except TimeoutError:
        return {"error": f"Pipeline excedeu o timeout de {timeout}s", "timeout": True}
    except ValueError as e:
        return {"error": str(e), "blocked": True}
    except Exception as e:
        return {"error": str(e)}


register_tool(
    ToolDefinition(
        name="execute_pipeline",
        description=(
            "Execute a shell command pipeline with pipes (|), logical operators "
            "(&&, ||, ;) and redirects (>, >>). Each command in the pipeline is "
            "validated individually. Example: 'cat server.log | grep ERROR | wc -l'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pipeline": {
                    "type": "string",
                    "description": "Full pipeline. E.g. 'cat app.log | grep ERROR | sort | uniq -c | sort -rn | head -20'",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional, must be inside the workspace)",
                },
                "timeout": {
                    "type": "integer",
                    # #DM063: default efetivo = min(default, cap) — TOOL_TIMEOUTS
                    # ["pipeline"]=240 excede o proprio cap (120), entao o valor
                    # anunciado ao modelo e o que realmente vale apos o min().
                    "description": (
                        f"Timeout in seconds (max {TOOL_TIMEOUT_CAPS['pipeline']}). "
                        f"Default: {min(TOOL_TIMEOUTS['pipeline'], TOOL_TIMEOUT_CAPS['pipeline'])}"
                    ),
                    "default": min(TOOL_TIMEOUTS["pipeline"], TOOL_TIMEOUT_CAPS["pipeline"]),
                },
            },
            "required": ["pipeline"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.SHELL,
        executor=_execute_pipeline,
    )
)
