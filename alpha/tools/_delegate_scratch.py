"""Sub-agent scratch directory helpers (#082).

Extracted from delegate_tools.py — agent ID generation, scratch dir
creation, and directory snapshot for result reporting.
"""

import secrets
from datetime import datetime
from pathlib import Path

_SCRATCH_SUBDIR = Path(".alpha") / "runs"


def _new_agent_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"


def _create_scratch_dir(parent_workspace: str, agent_id: str) -> Path:
    # exist_ok=False — a same-id collision means two agents would share state;
    # fail loudly instead of silently merging.
    scratch = Path(parent_workspace) / _SCRATCH_SUBDIR / agent_id
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(exist_ok=False)
    return scratch


def _snapshot_dir(path: Path) -> list[str]:
    if not path.exists():
        return []
    files = []
    for p in path.rglob("*"):
        if p.is_file():
            try:
                p.stat()
            except OSError:
                continue
            files.append(str(p.relative_to(path)))
    return sorted(files)
