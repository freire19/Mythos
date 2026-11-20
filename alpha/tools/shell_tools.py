"""Shell execution tool for ALPHA agent.

Implements `execute_shell` (DESTRUCTIVE) — the agent's escape hatch when
no structured tool fits. The skinny surface (one tool, one function)
intentionally pushes the agent toward `git_operation`, `read_file`,
`execute_pipeline`, etc. for things those tools already handle safely.

Defense layers BEFORE subprocess spawn:

1. `HARD_BLOCKED_RE` (from `alpha.security`, combined regex per #D020)
   rejects patterns the security review marks "never run", like writes
   to /dev/disk, /etc/cron.d, /etc/sudoers.d. This is fail-closed —
   tested via `test_security_v10_v11_*`.
2. `validate_command()` (from `alpha.security`) enforces a 75+ command
   allowlist + operator blacklist (`;`, `&`, backticks, `$()`); pipes
   handled separately via `validate_pipeline` (whitelisted operators
   like `|`, `>`, `>>` with redirect-target containment checks).
3. `assert_within_workspace(cwd)` — explicit `cwd` arg can't escape
   AGENT_WORKSPACE via `..` traversal or absolute path.
4. `get_safe_env()` strips LD_PRELOAD / LD_LIBRARY_PATH / PYTHONPATH
   from the child env (#D122) so a compromised env var can't pivot.

Execution: `run_subprocess_safe` from `_subprocess_helpers` (sync
subprocess timeout + `proc.kill()` on cancel/timeout, never `shell=True`).

Windows uses `_execute_shell_windows` shim (cmd.exe semantics) but with
the same security gates.

What's NOT here:
- Pipelines → `alpha.tools.pipeline_tools` (`execute_pipeline`)
- Subprocess primitives + timeout handling → `_subprocess_helpers`
- Security policy → `alpha.security`
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path

from .._platform import IS_WINDOWS
# AUDIT_V2.2 #021: removed the genuinely-dead imports HARD_BLOCKED,
# _HARD_BLOCKED_PATTERNS and validate_pipeline (referenced only in comments).
# HARD_BLOCKED_RE is kept as an intentional re-export: it's accessed as
# `shell_tools.HARD_BLOCKED_RE` by tests/test_perf_v10_stragglers.py.
from ..security import HARD_BLOCKED_RE, validate_command  # noqa: F401  (re-export)
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._subprocess_helpers import (
    STDERR_CAP,
    STDOUT_CAP,
    SubprocessTimeoutError,
    run_subprocess_safe,
)
from ..config import TOOL_TIMEOUTS, TOOL_TIMEOUT_CAPS
from .safe_env import get_safe_env
from .workspace import AGENT_WORKSPACE, assert_within_workspace

logger = logging.getLogger(__name__)

# Backward compat (#D002): _validate_command was refactored to
# alpha.security.validate_command. Alias kept so internal callers
# and tests don't break.
_validate_command = validate_command


# Comandos GUI que devem ser "fire-and-forget" (lançar e não esperar)
_GUI_COMMANDS = frozenset({"xdg-open", "xdg-mime", "notify-send"})


# ─── Tool ───


async def _execute_shell_windows(command: str, cwd: str, timeout: int) -> dict:
    """Execute via cmd.exe /c — necessario pra builtins (`dir`, `type`,
    `echo`), pipes e redirects, que `subprocess_exec` direto nao parseia.
    Injection vem so da string do comando, ja validada via HARD_BLOCKED_RE.
    """
    try:
        # Sandbox flag isn't applied on Windows — firejail/bubblewrap are
        # Linux-only. Documented in alpha/sandbox.py.
        r = await run_subprocess_safe(
            "cmd.exe", "/c", command, timeout=timeout, cwd=cwd,
        )
    except SubprocessTimeoutError:
        return {
            "error": f"Comando excedeu o timeout de {timeout}s",
            "timeout": True,
        }
    return {
        "exit_code": r.returncode,
        "stdout": r.stdout.decode(errors="replace")[:STDOUT_CAP],
        "stderr": r.stderr.decode(errors="replace")[:STDERR_CAP],
    }


async def _execute_shell(
    command: str, cwd: str | None = None, timeout: int | None = None,
    background: bool = False,
) -> dict:
    """Execute a shell command with timeout (or detached, when background)."""
    if timeout is None:
        timeout = TOOL_TIMEOUTS.get("shell", 180)
    # Validate command
    block_reason = _validate_command(command)
    if block_reason:
        return {"error": block_reason, "blocked": True}

    # Validate and restrict cwd
    if cwd:
        cwd_path = Path(cwd).expanduser().resolve()
        err = assert_within_workspace(cwd_path)
        if err:
            return {"error": err}
        cwd = str(cwd_path)
    else:
        cwd = str(AGENT_WORKSPACE)

    # Background: registra no bg_tasks (log em .alpha/runs/bg/, notificação
    # de término no turno seguinte). Validação de comando/cwd JÁ aconteceu.
    if background:
        if IS_WINDOWS:
            return {"error": "background=true não suportado no Windows ainda"}
        # #100: aplicar o MESMO wrap de sandbox do foreground ANTES de detachar.
        # Fail-closed: se o sandbox está on mas sem backend, propaga o erro em
        # vez de rodar o comando cru fora da jaula (o background era fail-open).
        from ..bg_tasks import start as _bg_start
        from ..shell_sandbox import SandboxUnavailableError, wrap_command
        try:
            cmd_parts = shlex.split(command)
            sandboxed = shlex.join(wrap_command(cmd_parts, cwd=cwd))
        except SandboxUnavailableError as e:
            return {"error": str(e), "blocked": True}
        except ValueError as e:
            return {"error": f"Comando malformado: {e}"}
        return _bg_start(sandboxed, cwd, env=get_safe_env())

    # Cap timeout (#D003: fonte unica em config.TOOL_TIMEOUT_CAPS)
    from ..config import TOOL_TIMEOUT_CAPS
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return {"error": f"timeout inválido: {timeout!r} (esperado número inteiro)"}
    timeout = min(timeout, TOOL_TIMEOUT_CAPS.get("shell", 300))

    try:
        if IS_WINDOWS:
            return await _execute_shell_windows(command, cwd, timeout)

        try:
            cmd_parts = shlex.split(command)
        except ValueError as e:
            return {"error": f"Comando malformado: {e}"}

        base_cmd = Path(cmd_parts[0]).name

        # GUI commands: detach (fire-and-forget) — não capturar output
        if base_cmd in _GUI_COMMANDS:
            # #D324: o spawn GUI-detach não passa por wrap_command; rodá-lo com o
            # sandbox ativo violaria a contenção pedida (e envolvê-lo em bwrap/
            # firejail sem D-Bus/display quebraria xdg-open/notify-send). Fail-
            # closed: recusa quando o sandbox está habilitado.
            from ..shell_sandbox import load_config as _load_sandbox_config
            if _load_sandbox_config().enabled:
                return {
                    "error": (
                        f"'{base_cmd}' (GUI detach) é bloqueado com o sandbox "
                        "habilitado — roda fora da jaula. Desabilite o sandbox "
                        "ou rode o comando manualmente."
                    ),
                    "blocked": True,
                }
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=get_safe_env(),
                start_new_session=True,  # desanexar do processo pai
            )
            return {
                "exit_code": 0,
                "stdout": f"Comando '{base_cmd}' lançado em background (PID {proc.pid})",
                "stderr": "",
                "detached": True,
            }

        # Execute pipes safely via chained subprocess_exec (NEVER use subprocess_shell)
        # #DL037: detecta pipe via shlex.parse (cmd_parts) — "|" como token
        # standalone = pipe real; "|" dentro de aspas = literal, preservado.
        has_pipe = "|" in cmd_parts
        if has_pipe:
            # Reconstroi segmentos do pipe a partir dos tokens parseados,
            # respeitando quoting (shlex.join preserva aspas necessarias).
            pipe_segments: list[str] = []
            current: list[str] = []
            for part in cmd_parts:
                if part == "|":
                    if current:
                        pipe_segments.append(shlex.join(current))
                        current = []
                else:
                    current.append(part)
            if current:
                pipe_segments.append(shlex.join(current))
            prev_output = None
            all_stderr = b""
            last_returncode = 0

            for seg in pipe_segments:
                try:
                    seg_parts = shlex.split(seg)
                except ValueError:
                    return {"error": f"Segmento malformado no pipe: {seg}"}
                if not seg_parts:
                    continue

                try:
                    r = await run_subprocess_safe(
                        *seg_parts, timeout=timeout, cwd=cwd,
                        stdin=prev_output, sandbox=True,
                    )
                except SubprocessTimeoutError:
                    return {
                        "error": f"Comando excedeu o timeout de {timeout}s",
                        "timeout": True,
                    }
                prev_output = r.stdout
                all_stderr += r.stderr
                last_returncode = r.returncode

            return {
                "exit_code": last_returncode,
                "stdout": (prev_output or b"").decode(errors="replace")[:STDOUT_CAP],
                "stderr": all_stderr.decode(errors="replace")[:STDERR_CAP],
            }
        else:
            try:
                r = await run_subprocess_safe(
                    *cmd_parts, timeout=timeout, cwd=cwd, sandbox=True,
                )
            except SubprocessTimeoutError:
                return {
                    "error": f"Comando excedeu o timeout de {timeout}s",
                    "timeout": True,
                }

            return {
                "exit_code": r.returncode,
                "stdout": r.stdout.decode(errors="replace")[:STDOUT_CAP],
                "stderr": r.stderr.decode(errors="replace")[:STDERR_CAP],
            }
    except Exception as e:
        # AUDIT_V2.2 #016: log with traceback and surface the error type —
        # a bare str(e) on a spawn failure (binary missing in sandbox,
        # permission denied, OOM at fork) left no trace in the logs.
        logger.exception("execute_shell failed: %s", str(command)[:120])
        return {"error": f"{type(e).__name__}: {e}"}


register_tool(
    ToolDefinition(
        name="execute_shell",
        description=(
            "Execute a shell command. Pipes (|) are supported — each "
            "segment is validated against catastrophic patterns. "
            "For && / || / ; / redirects (>, 2>) use execute_pipeline. "
            "Maximum timeout: 300s. Returns stdout, stderr, exit_code. "
            "Set background=true for long-running commands (dev servers, "
            "builds, watchers): returns a task_id immediately; you'll be "
            "notified when it finishes — check with bg_task_output/"
            "bg_tasks_list instead of blocking."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional, must be inside the workspace)",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        f"Timeout in seconds (max {TOOL_TIMEOUT_CAPS['shell']}). "
                        f"Default: {TOOL_TIMEOUTS['shell']}"
                    ),
                    "default": TOOL_TIMEOUTS["shell"],
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run detached and return a task_id immediately "
                        "(no pipes). Default: false"
                    ),
                    "default": False,
                },
            },
            "required": ["command"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.SHELL,
        executor=_execute_shell,
    )
)
