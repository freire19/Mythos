"""Git worktree isolation for sub-agents (parity wave 2026-11, item 5).

`delegate_task(isolation="worktree")` / `delegate_parallel(isolation=
"worktree")` run each sub-agent in its OWN checkout: a `git worktree` under
`<repo>/.alpha/runs/worktrees/<agent-id>` on a fresh branch
`alpha/<agent-id>` cut from HEAD. The sub-agent's workspace boundary becomes
that directory, so parallel agents editing the same files never collide and
the main checkout is untouched while they run.

After the run:
- clean worktree (no changes vs. the branch point) → worktree AND branch are
  removed, nothing to review;
- dirty worktree → kept; the result carries `worktree.path`, `worktree.branch`
  and `worktree.changed_files` so the parent (or the user) can diff, cherry-
  pick or `git worktree remove` it. Nothing is merged automatically.

`.alpha/runs/` is already gitignored, so the nested checkouts never show up
as untracked files in the main tree. Requires a git repo and the `git`
binary; both failures are reported as a tool error, never raised.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .tools._subprocess_helpers import SubprocessTimeoutError, run_subprocess_safe

logger = logging.getLogger(__name__)

WORKTREES_SUBDIR = Path(".alpha") / "runs" / "worktrees"
BRANCH_PREFIX = "alpha/"
_GIT_TIMEOUT = 60


class WorktreeError(RuntimeError):
    """Worktree could not be created/inspected/removed. Message is user-facing."""


async def _git(args: list[str], cwd: str) -> tuple[int, str, str]:
    if shutil.which("git") is None:
        raise WorktreeError("`git` não encontrado no PATH — isolation=worktree indisponível.")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = await run_subprocess_safe("git", *args, timeout=_GIT_TIMEOUT, cwd=cwd, env=env)
    except SubprocessTimeoutError as e:
        raise WorktreeError(f"git {' '.join(args[:2])} excedeu {_GIT_TIMEOUT}s") from e
    return (
        r.returncode,
        r.stdout.decode(errors="replace"),
        r.stderr.decode(errors="replace"),
    )


async def repo_root(path: str) -> str:
    """Top-level directory of the git repo containing `path`, or raise."""
    code, out, err = await _git(["rev-parse", "--show-toplevel"], path)
    if code != 0:
        raise WorktreeError(
            f"'{path}' não está num repositório git ({err.strip() or 'rev-parse falhou'}) — "
            "isolation=worktree exige um repo."
        )
    return out.strip()


async def create(workspace: str, agent_id: str) -> dict:
    """Create `<root>/.alpha/runs/worktrees/<agent_id>` on branch
    `alpha/<agent_id>` from HEAD. Returns {"path", "branch", "root"}."""
    root = await repo_root(workspace)
    code, _, err = await _git(["rev-parse", "--verify", "HEAD"], root)
    if code != 0:
        raise WorktreeError("repo sem commits (HEAD inválido) — faça um commit inicial antes.")
    wt_path = Path(root) / WORKTREES_SUBDIR / agent_id
    if wt_path.exists():
        raise WorktreeError(f"worktree já existe: {wt_path}")
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"{BRANCH_PREFIX}{agent_id}"
    code, _, err = await _git(
        ["worktree", "add", "-b", branch, str(wt_path), "HEAD"], root,
    )
    if code != 0:
        raise WorktreeError(f"git worktree add falhou: {err.strip()[:300]}")
    logger.info("worktree criado para sub-agente %s em %s (%s)", agent_id, wt_path, branch)
    return {"path": str(wt_path), "branch": branch, "root": root}


async def changed_files(wt_path: str) -> list[str]:
    """Files modified/added/deleted/untracked inside the worktree."""
    code, out, err = await _git(["status", "--porcelain", "--untracked-files=all"], wt_path)
    if code != 0:
        raise WorktreeError(f"git status falhou no worktree: {err.strip()[:300]}")
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip()
        if " -> " in rel:  # rename: keep the new name
            rel = rel.split(" -> ", 1)[1]
        # Ignore the sub-agent's own scratch dir inside the worktree.
        if rel.startswith(".alpha/"):
            continue
        files.append(rel)
    return files


async def remove(root: str, wt_path: str, branch: str | None, *, force: bool = False) -> None:
    """Remove the worktree (and its branch when given). `force` discards
    uncommitted changes. Best-effort: logs instead of raising."""
    args = ["worktree", "remove", str(wt_path)]
    if force:
        args.append("--force")
    code, _, err = await _git(args, root)
    if code != 0:
        logger.warning("git worktree remove falhou (%s): %s", wt_path, err.strip()[:200])
        return
    if branch:
        code, _, err = await _git(["branch", "-D", branch], root)
        if code != 0:
            logger.warning("git branch -D %s falhou: %s", branch, err.strip()[:200])


async def finalize(info: dict) -> dict:
    """Post-run: drop a clean worktree, keep a dirty one. Returns the
    `worktree` block for the sub-agent result."""
    try:
        changed = await changed_files(info["path"])
    except WorktreeError as e:
        return {**info, "kept": True, "error": str(e)}
    if not changed:
        await remove(info["root"], info["path"], info["branch"], force=True)
        return {"branch": info["branch"], "kept": False, "changed_files": []}
    return {
        "path": info["path"],
        "branch": info["branch"],
        "kept": True,
        "changed_files": changed,
        "hint": (
            f"Mudanças ficaram no branch {info['branch']} (worktree {info['path']}). "
            f"Revise com `git diff HEAD...{info['branch']}`; traga com merge/cherry-pick; "
            f"descarte com `git worktree remove --force {info['path']} && "
            f"git branch -D {info['branch']}`."
        ),
    }
