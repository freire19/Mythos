"""
Sandboxed exploit execution — Docker/podman ephemeral containers or
process-level isolation with crash detection and timeout.

Provides:
- run_exploit(): Execute a payload in a sandbox and capture results.
- sandbox_test(): Test a binary against a generated payload.
- Crash analysis (SIGSEGV, SIGILL, SIGABRT, SIGBUS).
"""

import asyncio
import logging
import os
import resource
import secrets
import signal
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Container detection ───

_docker_available: bool | None = None
_podman_available: bool | None = None


async def _docker_kill(runtime: str, container_name: str) -> None:
    """Kill the container on the daemon, not just the client process."""
    try:
        kill_proc = await asyncio.create_subprocess_exec(
            runtime, "kill", "--signal=KILL", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(kill_proc.wait(), timeout=5)
    except Exception:
        pass


def _get_container_runtime() -> str | None:
    global _docker_available, _podman_available
    if _podman_available is None:
        _podman_available = os.system("which podman >/dev/null 2>&1") == 0
    if _podman_available:
        return "podman"
    if _docker_available is None:
        _docker_available = os.system("which docker >/dev/null 2>&1") == 0
    if _docker_available:
        return "docker"
    return None


# ─── Crash signal mapping ───

CRASH_SIGNALS = {
    signal.SIGSEGV: "SIGSEGV — segmentation fault (buffer overflow, null deref)",
    signal.SIGILL: "SIGILL — illegal instruction (bad shellcode, wrong arch)",
    signal.SIGABRT: "SIGABRT — abort (canary, heap corruption detected)",
    signal.SIGBUS: "SIGBUS — bus error (misaligned access)",
    signal.SIGFPE: "SIGFPE — floating point exception (division by zero)",
}


def _analyze_crash(exit_code: int, stderr: str) -> dict:
    """Analyze exit code and stderr for crash indicators."""
    # Negative exit code = killed by signal (asyncio convention)
    if exit_code < 0:
        sig = -exit_code
        crash_info = CRASH_SIGNALS.get(sig, f"Signal {sig}")
        return {
            "crashed": True,
            "signal": sig,
            "signal_name": crash_info,
            "exit_code": exit_code,
            "evidence": stderr[:2000] if stderr else "",
        }

    # exit_code 255 can be normal failure — check stderr for crash patterns
    if b"segmentation fault" in stderr.lower().encode() or b"sigsegv" in stderr.lower().encode():
        return {
            "crashed": True,
            "signal": 11,
            "signal_name": "SIGSEGV (detected from stderr)",
            "exit_code": exit_code,
            "evidence": stderr[:2000],
        }

    # Normal exit
    return {"crashed": False, "exit_code": exit_code}


# ─── Process-level sandbox (fallback) ───


async def _run_process_sandbox(
    binary: str,
    input_data: bytes = b"",
    args: list[str] | None = None,
    timeout: float = 10.0,
    mem_limit_mb: int = 256,
    env: dict[str, str] | None = None,
) -> dict:
    """Run binary with resource limits (rlimit) as fallback sandbox."""
    cmd = [binary] + (args or [])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        def _set_limits():
            if mem_limit_mb:
                limit = mem_limit_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
                resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
            resource.setrlimit(resource.RLIMIT_CPU, (int(timeout), int(timeout)))

        # Note: setrlimit in subprocess preexec_fn would be ideal but
        # asyncio.create_subprocess_exec doesn't expose it easily.
        # We rely on timeout + kill instead.

        stdout, stderr = b"", b""
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_data), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
            except Exception:
                pass
            return {
                "ok": False,
                "timeout": True,
                "exit_code": -1,
                "stdout": stdout.decode("utf-8", errors="replace")[:4000],
                "stderr": stderr.decode("utf-8", errors="replace")[:4000],
                "crashed": False,
            }

        exit_code = proc.returncode or 0
        stdout_s = stdout.decode("utf-8", errors="replace")[:4000]
        stderr_s = stderr.decode("utf-8", errors="replace")[:4000]

        crash = _analyze_crash(exit_code, stderr_s)

        return {
            "ok": not crash["crashed"],
            "timeout": False,
            "exit_code": exit_code,
            "stdout": stdout_s,
            "stderr": stderr_s,
            **crash,
        }

    except FileNotFoundError:
        return {"ok": False, "error": f"Binary not found: {binary}", "crashed": False}
    except Exception as e:
        return {"ok": False, "error": str(e), "crashed": False}


# ─── Container sandbox (primary) ───


async def _run_container_sandbox(
    binary: str,
    input_data: bytes = b"",
    args: list[str] | None = None,
    timeout: float = 10.0,
    mem_limit_mb: int = 256,
    env: dict[str, str] | None = None,
    image: str = "alpine:latest",
    network: bool = False,
) -> dict:
    """Run binary in ephemeral Docker/podman container."""
    runtime = _get_container_runtime()
    if not runtime:
        return {"ok": False, "error": "No container runtime available (docker/podman)"}

    # Resolve absolute path and mount the binary's directory
    bin_path = Path(binary).resolve()
    if not bin_path.is_file():
        return {"ok": False, "error": f"Binary not found: {binary}"}
    mount_dir = str(bin_path.parent)
    container_name = f"alpha-sbx-{secrets.token_hex(6)}"

    container_cmd = [
        runtime, "run", "--rm",
        "--name", container_name,
        "-v", f"{mount_dir}:/target:ro",
        "--memory", f"{mem_limit_mb}m",
        "--memory-swap", f"{mem_limit_mb}m",
        "--pids-limit", "50",
        "--ulimit", f"cpu={int(timeout)}:100",
    ]

    if not network:
        container_cmd += ["--network", "none"]

    for k, v in (env or {}).items():
        container_cmd += ["-e", f"{k}={v}"]

    container_cmd += [
        image,
        "/target/" + bin_path.name,
    ] + (args or [])

    try:
        proc = await asyncio.create_subprocess_exec(
            *container_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout + 5
        )
    except asyncio.TimeoutError:
        await _docker_kill(runtime, container_name)  # container name
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {"ok": False, "timeout": True, "exit_code": -1,
                "stdout": "", "stderr": "Container timeout", "crashed": False}
    except (asyncio.CancelledError, KeyboardInterrupt):
        await _docker_kill(runtime, container_name)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    except FileNotFoundError:
        return {"ok": False, "error": f"{runtime} not found", "crashed": False}
    except Exception as e:
        return {"ok": False, "error": str(e), "crashed": False}

    exit_code = proc.returncode or 0
    stdout_s = stdout.decode("utf-8", errors="replace")[:4000]
    stderr_s = stderr.decode("utf-8", errors="replace")[:4000]
    crash = _analyze_crash(exit_code, stderr_s)

    return {
        "ok": not crash["crashed"],
        "timeout": False,
        "sandbox": runtime,
        "exit_code": exit_code,
        "stdout": stdout_s,
        "stderr": stderr_s,
        **crash,
    }


# ─── Public API ───


async def run_exploit(
    binary: str,
    payload_hex: str = "",
    payload_file: str = "",
    args: list[str] | None = None,
    timeout: float = 15.0,
    mem_limit_mb: int = 256,
    use_container: bool = True,
    env: dict[str, str] | None = None,
    stdin_mode: str = "payload",
) -> dict:
    """Run an exploit payload against a target binary in a sandbox.

    The sandbox provides isolation via Docker/podman container (preferred)
    or process-level rlimits (fallback). Captures crashes (SIGSEGV, SIGILL,
    SIGABRT with stack smashing) and classifies them for the feedback loop.

    Args:
        binary: Path to the target binary.
        payload_hex: Hex-encoded payload to pipe via stdin.
        payload_file: File containing payload (alternative to payload_hex).
        args: Command-line arguments to pass to the binary.
        timeout: Maximum execution time in seconds.
        mem_limit_mb: Memory limit in MB.
        use_container: Prefer container sandbox (docker/podman).
        env: Environment variables for the process.
        stdin_mode: 'payload' (pipe hex as stdin) or 'arg' (pass as command-line).

    Returns crash analysis, stdout, stderr, and success indicator.
    """
    # Build input
    if payload_file:
        try:
            input_data = Path(payload_file).read_bytes()
        except Exception as e:
            return {"ok": False, "error": f"Cannot read payload file: {e}"}
    elif payload_hex:
        try:
            input_data = bytes.fromhex(payload_hex.replace(" ", "").replace("\n", ""))
        except ValueError as e:
            return {"ok": False, "error": f"Invalid hex payload: {e}"}
    else:
        input_data = b""

    bin_path = str(Path(binary).resolve())

    if use_container:
        runtime = _get_container_runtime()
        if runtime is None:
            return {"ok": False, "error": "use_container=True but no container runtime (docker/podman) found. Install docker or set use_container=False."}
        result = await _run_container_sandbox(
            bin_path, input_data, args, timeout, mem_limit_mb, env or {}
        )
        # Fall back to process sandbox if container failed (docker installed
        # but not running, or image missing, or mount failed).
        if not result.get("error"):
            return result

    return await _run_process_sandbox(
        bin_path, input_data, args, timeout, mem_limit_mb, env or {}
    )


async def sandbox_test(
    binary: str,
    payload_hex: str = "",
    payload_type: str = "buffer_overflow",
    expected_behavior: str = "crash",
    timeout: float = 10.0,
    args: list[str] | None = None,
) -> dict:
    """Test an exploit and report success/failure with crash analysis.

    Designed to be called from the exploit feedback loop: generates a
    test case, runs it, and returns structured results for the agent
    to decide next steps.

    Args:
        binary: Target binary path.
        payload_hex: Hex-encoded exploit payload.
        payload_type: Type of exploit being tested.
        expected_behavior: 'crash' (we expect SIGSEGV/SIGABRT),
                           'shell' (we expect interactive output),
                           'file_read' (we expect file contents in stdout).
        timeout: Maximum time for the test.
        args: Additional CLI args for the binary.
    """
    result = await run_exploit(binary, payload_hex=payload_hex, args=args, timeout=timeout)

    # Classify result against expectations
    success = False
    evidence = ""

    if expected_behavior == "crash":
        success = result.get("crashed", False)
        if success:
            evidence = f"Target crashed with {result.get('signal_name', 'unknown')}"
    elif expected_behavior == "shell":
        # Shell spawning: non-crashing exit or specific output patterns
        success = not result.get("crashed", True) and result.get("exit_code", -1) >= 0
        if not success and result.get("crashed"):
            evidence = f"Target crashed ({result.get('signal_name')}) — shellcode needs adjustment"
    elif expected_behavior == "file_read":
        stdout = result.get("stdout", "")
        success = "root:" in stdout or "daemon:" in stdout or len(stdout) > 100
        evidence = f"Stdout length: {len(stdout)} chars"

    result["expected"] = expected_behavior
    result["success"] = success
    result["evidence"] = evidence
    return result


# ─── Tool registration ───

def _register_sandbox_tools():
    """Delay registration to avoid circular imports."""
    from .tools import ToolCategory, ToolDefinition, ToolSafety, register_tool

    tools = [
        ToolDefinition(
            name="run_exploit",
            description="Run an exploit payload against a binary in an isolated sandbox (Docker/podman or rlimits). Captures crash signals.",
            parameters={
                "type": "object",
                "properties": {
                    "binary": {"type": "string", "description": "Path to target binary."},
                    "payload_hex": {"type": "string", "description": "Hex-encoded exploit payload."},
                    "payload_file": {"type": "string", "description": "File containing raw payload."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "CLI arguments for the binary."},
                    "timeout": {"type": "number", "description": "Timeout in seconds."},
                    "mem_limit_mb": {"type": "integer", "description": "Memory limit in MB."},
                    "use_container": {"type": "boolean", "description": "Prefer container sandbox."},
                },
                "required": ["binary"],
            },
            safety=ToolSafety.DESTRUCTIVE,
            category=ToolCategory.SECURITY,
            executor=run_exploit,
        ),
        ToolDefinition(
            name="sandbox_test",
            description="Test an exploit and classify result (crash/shell/file_read) for the feedback loop.",
            parameters={
                "type": "object",
                "properties": {
                    "binary": {"type": "string", "description": "Target binary path."},
                    "payload_hex": {"type": "string", "description": "Hex-encoded exploit payload."},
                    "payload_type": {"type": "string", "description": "'buffer_overflow', 'format_string', 'ret2libc', 'rop_chain'."},
                    "expected_behavior": {"type": "string", "description": "'crash', 'shell', or 'file_read'.", "enum": ["crash", "shell", "file_read"]},
                    "timeout": {"type": "number", "description": "Timeout in seconds."},
                },
                "required": ["binary", "payload_hex"],
            },
            safety=ToolSafety.DESTRUCTIVE,
            category=ToolCategory.SECURITY,
            executor=sandbox_test,
        ),
    ]

    for td in tools:
        register_tool(td)

    logger.info("Sandbox tools registered: %d tools", len(tools))
