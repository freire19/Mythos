"""Shell execution tool for ALPHA agent."""

import asyncio
import shlex
from pathlib import Path

from .._platform import IS_WINDOWS
from ..security import (
    HARD_BLOCKED,
    HARD_BLOCKED_RE,
    _HARD_BLOCKED_PATTERNS,
    validate_command,
    validate_pipeline,
)
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._subprocess_helpers import SubprocessTimeoutError, run_subprocess_safe
from ..config import TOOL_TIMEOUTS
from .safe_env import get_safe_env
from .workspace import AGENT_WORKSPACE, assert_within_workspace

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
        "stdout": r.stdout.decode(errors="replace")[:15000],
        "stderr": r.stderr.decode(errors="replace")[:5000],
    }


async def _execute_shell(command: str, cwd: str = None, timeout: int | None = None) -> dict:
    """Execute a shell command with timeout."""
    if timeout is None:
        timeout = TOOL_TIMEOUTS.get("shell", 30)
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

    # Cap timeout (#D003: fonte unica em config.TOOL_TIMEOUT_CAPS)
    from ..config import TOOL_TIMEOUT_CAPS
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
                        stdin=prev_output,
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
                "stdout": (prev_output or b"").decode(errors="replace")[:15000],
                "stderr": all_stderr.decode(errors="replace")[:5000],
            }
        else:
            try:
                r = await run_subprocess_safe(
                    *cmd_parts, timeout=timeout, cwd=cwd,
                )
            except SubprocessTimeoutError:
                return {
                    "error": f"Comando excedeu o timeout de {timeout}s",
                    "timeout": True,
                }

            return {
                "exit_code": r.returncode,
                "stdout": r.stdout.decode(errors="replace")[:15000],
                "stderr": r.stderr.decode(errors="replace")[:5000],
            }
    except Exception as e:
        return {"error": str(e)}


register_tool(
    ToolDefinition(
        name="execute_shell",
        description=(
            "Executar um comando shell. Pipes (|) são suportados — cada "
            "segmento e validado contra padrões catastróficos. "
            "Para && / || / ; / redirects (>, 2>) use execute_pipeline. "
            "Timeout máximo: 300s. Retorna stdout, stderr, exit_code."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Comando shell a executar"},
                "cwd": {
                    "type": "string",
                    "description": "Diretório de trabalho (opcional, deve estar dentro do workspace)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout em segundos (máx 300). Padrão: 30",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.SHELL,
        executor=_execute_shell,
    )
)
