"""Regression guard for `alpha/prompts/system.md` (Plano-Upgrade-v3 H2 #11).

The system prompt is the most critical asset in the repo — it shapes
every agent turn. Without a snapshot test, edits land silently. With
this, any change shows up as a failing test whose diff is the whole
change set, making intent explicit in PR review.

Workflow when intentionally changing the prompt:
    cp alpha/prompts/system.md tests/snapshots/system_prompt.md
    git add tests/snapshots/system_prompt.md
    # commit both files together; PR shows the diff cleanly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_PROMPT = REPO_ROOT / "alpha" / "prompts" / "system.md"
SNAPSHOT = Path(__file__).parent / "snapshots" / "system_prompt.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_system_prompt_matches_snapshot():
    """Fails when prompts/system.md drifts from tests/snapshots/system_prompt.md.

    On failure: review the diff intentionally, then refresh the snapshot
    (see module docstring). Do not just rerun until green — the whole
    point is to make prompt edits a visible part of the PR."""
    assert LIVE_PROMPT.exists(), f"missing {LIVE_PROMPT}"
    assert SNAPSHOT.exists(), (
        f"missing {SNAPSHOT} — to seed, run:\n"
        f"  cp {LIVE_PROMPT} {SNAPSHOT}"
    )

    live_hash = _sha256(LIVE_PROMPT)
    snap_hash = _sha256(SNAPSHOT)
    if live_hash == snap_hash:
        return

    # Build a meaningful failure message: show line counts and the first
    # diverging line so the developer sees what to look at without
    # dumping the whole 300-line prompt into pytest output.
    live_lines = LIVE_PROMPT.read_text(encoding="utf-8").splitlines()
    snap_lines = SNAPSHOT.read_text(encoding="utf-8").splitlines()
    first_diff = next(
        (
            i for i, (a, b) in enumerate(zip(live_lines, snap_lines))
            if a != b
        ),
        min(len(live_lines), len(snap_lines)),
    )
    raise AssertionError(
        f"system.md changed:\n"
        f"  live    sha256={live_hash[:12]}…  lines={len(live_lines)}\n"
        f"  snapshot sha256={snap_hash[:12]}…  lines={len(snap_lines)}\n"
        f"  first diff at line {first_diff + 1}:\n"
        f"    live:     {live_lines[first_diff] if first_diff < len(live_lines) else '<EOF>'!r}\n"
        f"    snapshot: {snap_lines[first_diff] if first_diff < len(snap_lines) else '<EOF>'!r}\n"
        f"  if the change is intentional:\n"
        f"    cp {LIVE_PROMPT.relative_to(REPO_ROOT)} {SNAPSHOT.relative_to(REPO_ROOT)}"
    )


def test_snapshot_file_committed():
    """Ensures `tests/snapshots/system_prompt.md` is committed so CI runs
    can find it. The file's presence is the contract — empty snapshot
    means the test passes trivially, which defeats the purpose."""
    assert SNAPSHOT.exists(), "snapshot file must exist (see module docstring)"
    assert SNAPSHOT.stat().st_size > 0, "snapshot file must not be empty"
