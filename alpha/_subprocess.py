"""Safe async subprocess runner (#D001).

Encapsulates the pattern repeated in 10+ modules:
    proc = await asyncio.create_subprocess_exec(...)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill(); await proc.wait()
    except asyncio.CancelledError:
        proc.kill(); await proc.wait(); raise

Migrate callers by replacing the inline pattern with `await
run_subprocess(cmd, timeout=...)`.  Returns `(rc, stdout, stderr)`.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class SubprocessResult:
    """Result of a completed (or timed-out) subprocess run."""

    __slots__ = ("returncode", "stdout", "stderr", "timed_out")

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes, timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


async def run_subprocess(
    cmd: list[str],
    *,
    timeout: float = 30.0,
    cwd: str | None = None,
    env: dict | None = None,
    stdin_input: bytes | None = None,
) -> SubprocessResult:
    """Run a subprocess and wait for it with timeout and cancellation safety.

    Args:
        cmd: Command and arguments as a list of strings.
        timeout: Maximum seconds to wait for the process.
        cwd: Working directory (optional).
        env: Environment dict (optional).
        stdin_input: Bytes to write to stdin (optional).

    Returns:
        SubprocessResult with returncode, stdout, stderr, and timed_out flag.

    Raises:
        asyncio.CancelledError: Propagated after killing the child process.
    """
    kwargs: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

    try:
        communicate_kwargs = {"input": stdin_input} if stdin_input is not None else {}
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(**communicate_kwargs), timeout=timeout
        )
        return SubprocessResult(
            returncode=proc.returncode or 0,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return SubprocessResult(
            returncode=-1,
            stdout=b"",
            stderr=b"",
            timed_out=True,
        )
    except asyncio.CancelledError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise
