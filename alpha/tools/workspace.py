"""Shared workspace path for all agent tools (single source of truth).

Defaults to current working directory (like Claude Code).
Set AGENT_WORKSPACE env var to override.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# #D139: exact-match alone let `AGENT_WORKSPACE=/proc/self` or `/etc/x` slip
# through. Split into two checks so subdirs of TRUE system dirs are rejected
# WITHOUT breaking legitimate workspaces that live under /home, /opt, /mnt, …
#
# - prefix-forbidden: nothing under these should ever be a workspace.
_FORBIDDEN_PREFIXES = (
    Path("/proc"), Path("/sys"), Path("/dev"), Path("/etc"),
    Path("/boot"), Path("/bin"), Path("/sbin"), Path("/lib"),
    Path("/lib64"), Path("/run"), Path("/snap"),
)
# - exact-forbidden: the dir itself is wrong as a workspace root, but its
#   subdirs are normal (/home/<user>/proj, /opt/app, /mnt/data/proj, …).
_FORBIDDEN_EXACT = frozenset({
    Path("/"), Path("/home"), Path("/root"), Path("/tmp"), Path("/var"),
    Path("/usr"), Path("/opt"), Path("/mnt"), Path("/media"), Path("/srv"),
})


def _is_forbidden_workspace(p: Path) -> bool:
    """True if `p` is a system dir (exact) or lives under one (prefix) (#D139)."""
    if p in _FORBIDDEN_EXACT:
        return True
    return any(p == pre or p.is_relative_to(pre) for pre in _FORBIDDEN_PREFIXES)


_raw_workspace = Path(os.getenv("AGENT_WORKSPACE", os.getcwd())).resolve()

if _is_forbidden_workspace(_raw_workspace):
    logger.warning(
        f"AGENT_WORKSPACE={_raw_workspace} is (under) a system directory — falling back to CWD"
    )
    AGENT_WORKSPACE = Path.cwd().resolve()
else:
    AGENT_WORKSPACE = _raw_workspace

AGENT_WORKSPACE.mkdir(parents=True, exist_ok=True)


def assert_within_workspace(path: Path | str) -> str | None:
    """Validate that `path` is inside AGENT_WORKSPACE.

    Centraliza o check `path.relative_to(AGENT_WORKSPACE)` que estava
    duplicado em ~10 sites de tools (#DL015 DEEP_LOGIC). Single source
    para a Camada B do enforcement de workspace; reduz risco de drift
    quando o check muda em uma tool e nao em outras.

    Caller deve fazer `resolve()` ANTES se necessario (ex: symlink-aware
    checks). Esta funcao faz apenas `expanduser()` para manter o
    comportamento consistente com os sites originais onde alguns usam
    resolve e outros usam normpath.

    Returns None se OK, ou mensagem de erro se path estiver fora.
    """
    p = Path(path).expanduser()
    try:
        p.relative_to(AGENT_WORKSPACE)
        return None
    except ValueError:
        return f"Path fora do workspace permitido ({AGENT_WORKSPACE}): {p}"
