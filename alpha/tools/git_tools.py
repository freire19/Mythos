"""Git operations tool for ALPHA agent.

Provides safe, structured git operations within the workspace.

SECURITY: Only operates within AGENT_WORKSPACE. Destructive operations
(push, reset, clean) require approval. Read operations are safe.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._subprocess_helpers import SubprocessTimeoutError, run_subprocess_safe
from ..config import TOOL_TIMEOUTS
from .safe_env import get_safe_env
from .workspace import AGENT_WORKSPACE, assert_within_workspace

# #D006: pre-compilada no module level. Antes era recompilada em cada
# `_sanitize_git_args` (3-5 chamadas por tool call de log/show/diff/push/etc).
_DANGEROUS_GIT_FMT = re.compile(
    r"%\((if|then|else|end|contents:signature|trailers)\)", re.IGNORECASE
)

logger = logging.getLogger(__name__)

# Whitelist de flags permitidas por action
_ALLOWED_GIT_FLAGS = {
    "diff": {"--stat", "--name-only", "--cached", "--staged", "--shortstat", "--no-color"},
    "log": {
        "--oneline",
        "--graph",
        "--all",
        "-n",
        "--format",
        "--since",
        "--author",
        "--pretty",
        "--abbrev-commit",
        "--no-color",
    },
    "show": {"--stat", "--no-color", "--format", "--pretty"},
    "push": {"--set-upstream", "-u", "--tags"},
    "reset": {"--soft", "--mixed"},  # --hard requer aprovação via _needs_approval
    "tag_create": {"-a", "--annotate", "-m", "--message"},  # DL035
}

# Flags globalmente bloqueadas (escape do workspace / configuração)
_BLOCKED_GIT_FLAGS = frozenset({"--no-index", "--work-tree", "--git-dir", "-C", "--file"})

# Read-only git actions (SAFE)
_SAFE_ACTIONS = frozenset(
    {
        "status",
        "diff",
        "log",
        "branch",
        "show",
        "blame",
        "stash_list",
        "remote",
        "tag",
    }
)

# Write/mutating git actions (DESTRUCTIVE)
_DESTRUCTIVE_ACTIONS = frozenset(
    {
        "add",
        "commit",
        "checkout",
        "stash",
        "stash_pop",
        "pull",
        "push",
        "merge",
        "rebase",
        "reset",
        "clean",
        "branch_create",
        "branch_delete",
        "tag_create",
    }
)

_ALL_ACTIONS = _SAFE_ACTIONS | _DESTRUCTIVE_ACTIONS


async def _run_git(args: list[str], cwd: str, timeout: int | None = None) -> dict:
    """Run a git command and return result."""
    if timeout is None:
        timeout = TOOL_TIMEOUTS.get("git", 30)
    cmd = ["git"] + args
    try:
        r = await run_subprocess_safe(*cmd, timeout=timeout, cwd=cwd)
    except SubprocessTimeoutError:
        return {"error": f"git excedeu timeout de {timeout}s", "timeout": True}
    except Exception as e:
        return {"error": str(e)}

    return {
        "exit_code": r.returncode,
        "stdout": r.stdout.decode(errors="replace")[:15000],
        "stderr": r.stderr.decode(errors="replace")[:3000],
    }


def _find_git_repo(path: str) -> str | None:
    """Walk up to find .git directory. Never escapes AGENT_WORKSPACE."""
    p = Path(path).resolve()
    ws = AGENT_WORKSPACE.resolve()
    while p != p.parent:
        try:
            p.relative_to(ws)
        except ValueError:
            return None
        if (p / ".git").exists():
            return str(p)
        p = p.parent
    return None


def _sanitize_git_args(action: str, args: str) -> tuple[list[str], str | None]:
    """Valida e sanitiza args git. Retorna (args_limpos, erro_ou_None)."""
    if not args:
        return [], None

    try:
        parts = shlex.split(args)
    except ValueError:
        return [], "Args malformados"

    for part in parts:
        # Bloquear flags globalmente perigosas (match exato ou prefixo com =)
        for blocked in _BLOCKED_GIT_FLAGS:
            if part == blocked or part.startswith(f"{blocked}=") or part.startswith(f"{blocked}/"):
                return [], f"Flag '{blocked}' bloqueada por segurança"
        # Bloquear force push via +refspec
        if part.startswith("+") and action == "push":
            return [], "Force push via +refspec bloqueado"

    # Block dangerous format string expansions (can execute hooks).
    # Regex pre-compilada em module level (#D006) — evitar recompilacao a
    # cada chamada de _sanitize_git_args.
    for j, part in enumerate(parts):
        # Check --format=VALUE and --pretty=VALUE (with =)
        if part.startswith("--format=") or part.startswith("--pretty="):
            fmt_value = part.split("=", 1)[1]
            if _DANGEROUS_GIT_FMT.search(fmt_value):
                return [], f"Format string com expansões perigosas bloqueada: '{part[:50]}'"
        # Check --format VALUE and --pretty VALUE (space-separated)
        elif part in ("--format", "--pretty") and j + 1 < len(parts):
            next_val = parts[j + 1]
            if _DANGEROUS_GIT_FMT.search(next_val):
                return [], f"Format string com expansões perigosas bloqueada: '{next_val[:50]}'"

    # Se a action tem whitelist, validar flags
    allowed = _ALLOWED_GIT_FLAGS.get(action)
    if allowed is not None:
        for part in parts:
            if part.startswith("-"):
                # `--` is the standard path separator in git ("git diff -- path/").
                # Blocking it would block any path-scoped invocation.
                if part == "--":
                    continue
                # Permitir flags numéricas como -20 para log
                if part.lstrip("-").isdigit():
                    continue
                # Allow --format=... and --pretty=... (already validated above)
                flag_base = part.split("=")[0]
                if flag_base in ("--format", "--pretty") and "=" in part:
                    if flag_base in allowed:
                        continue
                if part not in allowed:
                    return [], f"Flag '{part}' não permitida para git {action}"

    return parts, None


def _reject_dash_prefixed(label: str, value: str) -> str | None:
    """Bloqueia values comecando com '-' que git interpretaria como flag.

    Sem isso, `branch="--detach"` em checkout vira flag (descartando local
    changes); `message="--amend"` em commit reescreve o ultimo commit;
    `files=["--exec=evil"]` em add executa hooks. subprocess_exec ja
    previne shell injection, mas nao protege contra arg-injection no
    proprio git.
    """
    if value and value.startswith("-"):
        return f"{label} não pode começar com '-' (interpretado como flag git): {value!r}"
    return None


# ── Action handlers (#DM044): elif chain de 22 acoes virou dict dispatch.
# Cada handler recebe so o que precisa via kwargs. Vantagens vs. elif:
# - Cada handler e unidade testavel sozinha
# - Adicionar acao = mais uma entrada em _GIT_ACTIONS (nao precisa achar
#   o lugar certo no meio de 180L de elif)
# - Validacao comum (resolve cwd, reject dash-prefixed) e DRY: vive em
#   `_git_operation` antes do dispatch, nao replica nas 22 ramificacoes


async def _git_status(*, cwd, **_):
    return await _run_git(["status", "--porcelain", "-b"], cwd)


async def _git_diff(*, cwd, args=None, **_):
    extra, err = _sanitize_git_args("diff", args)
    if err:
        return {"error": err}
    return await _run_git(["diff"] + extra, cwd)


async def _git_log(*, cwd, args=None, **_):
    extra, err = _sanitize_git_args("log", args)
    if err:
        return {"error": err}
    return await _run_git(["log"] + (extra or ["--oneline", "-20"]), cwd)


async def _git_branch(*, cwd, **_):
    return await _run_git(["branch", "-a", "-v"], cwd)


async def _git_show(*, cwd, args=None, **_):
    extra, err = _sanitize_git_args("show", args)
    if err:
        return {"error": err}
    # #DL035: extra pode conter flags + positional. Passa tudo pro git;
    # fallback HEAD quando vazio (evitava extra[0] que confundia flag com ref).
    return await _run_git(["show", "--stat"] + (extra or ["HEAD"]), cwd)


async def _git_blame(*, cwd, files=None, **_):
    if not files:
        return {"error": "blame requer 'files' com pelo menos um arquivo"}
    return await _run_git(["blame", "--porcelain", files[0]], cwd, timeout=60)


async def _git_stash_list(*, cwd, **_):
    return await _run_git(["stash", "list"], cwd)


async def _git_remote(*, cwd, **_):
    return await _run_git(["remote", "-v"], cwd)


async def _git_tag(*, cwd, **_):
    return await _run_git(["tag", "-l", "--sort=-creatordate"], cwd)


async def _git_add(*, cwd, files=None, **_):
    return await _run_git(["add"] + (files or ["."]), cwd)


async def _git_commit(*, cwd, message=None, **_):
    if not message:
        return {"error": "commit requer 'message'"}
    return await _run_git(["commit", "-m", message], cwd)


async def _git_checkout(*, cwd, branch=None, **_):
    if not branch:
        return {"error": "checkout requer 'branch'"}
    return await _run_git(["checkout", branch], cwd)


async def _git_branch_create(*, cwd, branch=None, **_):
    if not branch:
        return {"error": "branch_create requer 'branch'"}
    return await _run_git(["checkout", "-b", branch], cwd)


async def _git_branch_delete(*, cwd, branch=None, **_):
    if not branch:
        return {"error": "branch_delete requer 'branch'"}
    if branch in ("main", "master"):
        return {"error": "Não é permitido deletar branch main/master"}
    return await _run_git(["branch", "-d", branch], cwd)


async def _git_stash(*, cwd, message=None, **_):
    msg = ["-m", message] if message else []
    return await _run_git(["stash", "push"] + msg, cwd)


async def _git_stash_pop(*, cwd, **_):
    return await _run_git(["stash", "pop"], cwd)


async def _git_pull(*, cwd, **_):
    return await _run_git(["pull", "--rebase"], cwd, timeout=60)


async def _git_push(*, cwd, args=None, **_):
    extra, err = _sanitize_git_args("push", args)
    if err:
        return {"error": err}
    # Force push e bloqueado a priori: `_ALLOWED_GIT_FLAGS["push"]` so
    # permite `--set-upstream`, `-u`, `--tags`. Qualquer `--force`/`-f`
    # rejeitado pelo `_sanitize_git_args` antes de chegar aqui (#D029).
    # Se um dia force push for permitido em branches nao-main, lembrar
    # de adicionar a allowlist E reintroduzir o check de current branch.
    return await _run_git(["push"] + extra, cwd, timeout=60)


async def _git_merge(*, cwd, branch=None, **_):
    if not branch:
        return {"error": "merge requer 'branch'"}
    return await _run_git(["merge", branch], cwd)


async def _git_rebase(*, cwd, branch=None, **_):
    if not branch:
        return {"error": "rebase requer 'branch'"}
    return await _run_git(["rebase", branch], cwd)


async def _git_reset(*, cwd, args=None, **_):
    extra, err = _sanitize_git_args("reset", args)
    if err:
        return {"error": err}
    if not extra:
        extra = ["--mixed", "HEAD~1"]
    else:
        # Inject --mixed if no mode flag provided (avoid implicit git defaults)
        has_mode = any(f in extra for f in ("--soft", "--mixed", "--hard", "--merge", "--keep"))
        if not has_mode:
            extra = ["--mixed"] + extra
    return await _run_git(["reset"] + extra, cwd)


async def _git_clean(*, cwd, **_):
    return await _run_git(["clean", "-fd"], cwd)


async def _git_tag_create(*, cwd, args=None, message=None, **_):
    if not args:
        return {"error": "tag_create requer 'args' com o nome da tag"}
    tag_args, err = _sanitize_git_args("tag_create", args)
    if err:
        return {"error": err}
    if not tag_args:
        return {"error": "tag_create requer pelo menos o nome da tag"}
    # #DL036: tag_args may carry flags (-a, -m) before the positional
    # tag name. Pick the first non-flag token; refuse if there isn't one.
    tag_name = next((p for p in tag_args if not p.startswith("-")), None)
    if tag_name is None:
        return {"error": "tag_create requer um nome de tag positional"}
    if message:
        return await _run_git(["tag", "-a", tag_name, "-m", message], cwd)
    return await _run_git(["tag"] + tag_args, cwd)


_GIT_ACTIONS = {
    # Read-only (_SAFE_ACTIONS)
    "status": _git_status,
    "diff": _git_diff,
    "log": _git_log,
    "branch": _git_branch,
    "show": _git_show,
    "blame": _git_blame,
    "stash_list": _git_stash_list,
    "remote": _git_remote,
    "tag": _git_tag,
    # Write/mutating (_DESTRUCTIVE_ACTIONS)
    "add": _git_add,
    "commit": _git_commit,
    "checkout": _git_checkout,
    "branch_create": _git_branch_create,
    "branch_delete": _git_branch_delete,
    "stash": _git_stash,
    "stash_pop": _git_stash_pop,
    "pull": _git_pull,
    "push": _git_push,
    "merge": _git_merge,
    "rebase": _git_rebase,
    "reset": _git_reset,
    "clean": _git_clean,
    "tag_create": _git_tag_create,
}

# Invariant: dispatch table must cover the action enum exposed to the LLM.
# Drift here = HTTP 400 / silent failure at runtime.
assert set(_GIT_ACTIONS) == _ALL_ACTIONS, (
    f"_GIT_ACTIONS drift vs _ALL_ACTIONS: "
    f"missing={_ALL_ACTIONS - set(_GIT_ACTIONS)}, "
    f"extra={set(_GIT_ACTIONS) - _ALL_ACTIONS}"
)


def _resolve_git_cwd(path: str | None) -> tuple[str | None, dict | None]:
    """Resolve and validate workspace path, then find enclosing .git repo.

    Returns (cwd, None) on success or (None, error_dict) on failure.
    """
    if path:
        repo_path = Path(path).expanduser().resolve()
        err = assert_within_workspace(repo_path)
        if err:
            return None, {"error": err}
        cwd = str(repo_path)
    else:
        cwd = str(AGENT_WORKSPACE)

    repo_root = _find_git_repo(cwd)
    if not repo_root:
        return None, {"error": f"Nenhum repositório git encontrado em {cwd} ou diretórios pais"}
    return repo_root, None


def _validate_user_inputs(branch, message, files) -> str | None:
    """Reject values starting with '-' that git would treat as flags."""
    if branch is not None:
        if (err := _reject_dash_prefixed("branch", branch)):
            return err
    if message is not None:
        if (err := _reject_dash_prefixed("message", message)):
            return err
    if files:
        for f in files:
            if (err := _reject_dash_prefixed("files[]", f)):
                return err
    return None


async def _git_operation(
    action: str,
    path: str = None,
    message: str = None,
    branch: str = None,
    files: list = None,
    args: str = None,
) -> dict:
    """Execute a structured git operation via dispatch table (#DM044)."""
    action = action.lower().strip()
    handler = _GIT_ACTIONS.get(action)
    if handler is None:
        return {
            "error": f"Ação git '{action}' não reconhecida. "
            f"Ações disponíveis: {', '.join(sorted(_ALL_ACTIONS))}",
        }

    if (err := _validate_user_inputs(branch, message, files)):
        return {"error": err}

    cwd, cwd_err = _resolve_git_cwd(path)
    if cwd_err:
        return cwd_err

    return await handler(
        cwd=cwd, path=path, message=message, branch=branch, files=files, args=args
    )


# Register safe version (read-only operations)
register_tool(
    ToolDefinition(
        name="git_operation",
        description=(
            "Executar operações git de forma segura e estruturada. "
            "Ações de leitura: status, diff, log, branch, show, blame, stash_list, remote, tag. "
            "Ações de escrita: add, commit, checkout, branch_create, branch_delete, stash, "
            "stash_pop, pull, push, merge, rebase, reset, clean, tag_create. "
            "Force push em main/master é bloqueado."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Ação git a executar",
                    "enum": sorted(_ALL_ACTIONS),
                },
                "path": {
                    "type": "string",
                    "description": "Caminho do repositório (opcional, usa workspace padrão)",
                },
                "message": {
                    "type": "string",
                    "description": "Mensagem para commit, stash ou tag",
                },
                "branch": {
                    "type": "string",
                    "description": "Nome da branch (para checkout, merge, rebase, branch_create, branch_delete)",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista de arquivos (para add, blame)",
                },
                "args": {
                    "type": "string",
                    "description": "Argumentos extras como string (para diff, log, push, reset, show, tag_create)",
                },
            },
            "required": ["action"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.GIT,
        executor=_git_operation,
    )
)
