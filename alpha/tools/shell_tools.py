"""Shell execution tool for ALPHA agent."""

import asyncio
import shlex
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ..config import TOOL_TIMEOUTS, TOOL_TIMEOUT_CAPS
from ..security import HARD_BLOCKED_RE, validate_command
from .safe_env import get_safe_env
from .workspace import AGENT_WORKSPACE, assert_within_workspace


def _validate_command(command: str) -> str | None:
    """Return error message if command is destructive, None otherwise.

    Delegates to the central security hub for hard-blocked pattern checks,
    then adds syntactic sanity checks per pipe segment.
    """
    error = validate_command(command)
    if error:
        return error

    # Syntactic sanity check per pipe segment
    segments = command.split("|") if "|" in command else [command]
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        try:
            parts = shlex.split(segment)
            if not parts:
                continue
        except ValueError:
            return "Comando malformado"

    return None


# Comandos GUI que devem ser "fire-and-forget" (lançar e não esperar)
_GUI_COMMANDS = frozenset({"xdg-open", "xdg-mime", "notify-send"})


# ─── Tool ───


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
    timeout = min(timeout, TOOL_TIMEOUT_CAPS.get("shell", 300))

    try:
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
        has_pipe = "|" in command
        if has_pipe:
            pipe_segments = [s.strip() for s in command.split("|") if s.strip()]
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

                proc = await asyncio.create_subprocess_exec(
                    *seg_parts,
                    stdin=asyncio.subprocess.PIPE if prev_output is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=get_safe_env(),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(input=prev_output), timeout=timeout
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return {
                        "error": f"Comando excedeu o timeout de {timeout}s",
                        "timeout": True,
                    }
                except (asyncio.CancelledError, KeyboardInterrupt):
                    proc.kill()
                    await proc.wait()
                    raise
                prev_output = stdout
                all_stderr += stderr
                last_returncode = proc.returncode

            return {
                "exit_code": last_returncode,
                "stdout": (prev_output or b"").decode(errors="replace")[:15000],
                "stderr": all_stderr.decode(errors="replace")[:5000],
            }
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=get_safe_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "error": f"Comando excedeu o timeout de {timeout}s",
                    "timeout": True,
                }
            except (asyncio.CancelledError, KeyboardInterrupt):
                # Sem este bloco, Ctrl+C deixa o subprocess rodando ate o
                # fim (ex: `git push` ou `npm install` continua exfiltrando
                # apesar do REPL ter "cancelado").
                proc.kill()
                await proc.wait()
                raise

            return {
                "exit_code": proc.returncode,
                "stdout": stdout.decode(errors="replace")[:15000],
                "stderr": stderr.decode(errors="replace")[:5000],
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
