"""`/review` — review the working tree, a PR, a branch, a commit or a path.

Collects the diff locally (`alpha/review.py`), prints a one-line summary
and FALLS THROUGH with the review prompt as the user input. The history
records the slash line, not the whole diff.
"""

from __future__ import annotations

import os
import sys

from alpha import review
from alpha.display import C, c, print_error

from ._types import DispatchResult, ReplContext

_USAGE = (
    "/review                         working tree (staged + unstaged) vs HEAD\n"
    "  /review <PR#|PR url>            pull request via gh (needs `gh auth login`)\n"
    "  /review <branch> [--base <ref>] branch vs base (default origin/HEAD → main/master)\n"
    "  /review <commit>                a single commit\n"
    "  /review <path>                  only that file/dir\n"
    "  flags: --deep (3 parallel reviewers + verification), --fix (apply confirmed fixes),\n"
    "         --comment (post on the PR); trailing words = extra focus"
)


def _parse(args: list[str]) -> tuple[str, str | None, bool, bool, bool, str]:
    target = ""
    base: str | None = None
    fix = comment = deep = False
    focus: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--base":
            if i + 1 >= len(args):
                raise review.ReviewError("--base precisa de um ref")
            base = args[i + 1]
            i += 2
            continue
        if a.startswith("--base="):
            base = a.split("=", 1)[1]
        elif a == "--fix":
            fix = True
        elif a == "--comment":
            comment = True
        elif a == "--deep":
            deep = True
        elif a in ("-h", "--help", "help"):
            raise review.ReviewError("")
        elif a.startswith("--"):
            raise review.ReviewError(f"flag desconhecida: {a}")
        elif not target and not focus:
            target = a
        else:
            focus.append(a)
        i += 1
    return target, base, fix, comment, deep, " ".join(focus)


def build_from_args(args: list[str], cwd: str) -> tuple[str | None, review.ReviewTarget, str]:
    """Shared by the REPL handler and headless `alpha -p "/review …"`.

    Returns ``(prompt, target, summary)``; ``prompt`` is None when there is
    nothing to review. Warnings about ignored flags go to ``summary``.
    Raises ``ReviewError`` for bad arguments / unresolvable targets."""
    target, base, fix, comment, deep, focus = _parse(args)
    t = review.resolve(target, base=base, cwd=cwd)
    if t.empty:
        return None, t, f"Nada para revisar em {t.label}."
    notes: list[str] = []
    if comment and t.kind != "pr":
        notes.append("--comment só vale para PRs; ignorado.")
        comment = False
    deep_path = None
    if deep:
        from alpha.config import FEATURES
        if not (FEATURES.get("multi_agent_enabled") and FEATURES.get("delegate_tool_enabled")):
            notes.append("--deep precisa de delegação habilitada; revisão simples.")
            deep = False
        else:
            deep_path = review.save_diff(t, cwd)
    prompt = review.build_prompt(t, focus=focus, fix=fix, comment=comment, deep_path=deep_path)

    n_files = len(t.files) + (len(t.untracked) if t.kind == "worktree" else 0)
    extra = []
    if deep:
        extra.append(f"deep ({len(review.DEEP_DIMENSIONS)} reviewers)")
    if t.truncated:
        extra.append("diff truncado")
    if fix:
        extra.append("--fix")
    if comment:
        extra.append("--comment")
    tail = f" · {', '.join(extra)}" if extra else ""
    title = f' "{t.title[:60]}"' if t.title else ""
    summary = f"{t.label}{title} ({n_files} arquivo(s), {len(t.diff):,} chars{tail})"
    if notes:
        summary += "\n" + "\n".join(f"⚠ {n}" for n in notes)
    return prompt, t, summary


def _handle_review(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    try:
        prompt, _t, summary = build_from_args(parts[1:], os.getcwd())
    except review.ReviewError as e:
        if str(e):
            print_error(str(e))
        print(f"  {c(C.YELLOW, 'Usage:')} {_USAGE}")
        return DispatchResult.CONTINUE

    if prompt is None:
        print(f"  {c(C.GRAY, summary)}")
        return DispatchResult.CONTINUE
    first, *notes = summary.splitlines()
    for n in notes:
        print(f"  {c(C.YELLOW, n)}")
    ctx.user_input_override = prompt
    ctx.history_record_override = " ".join(parts)
    print(f"  {c(C.GREEN, '✦')} {c(C.CYAN, '/review')} {c(C.GRAY, '— ' + first)}")
    return DispatchResult.FALL_THROUGH


def expand_headless(message: str, cwd: str) -> str | None:
    """`alpha -p "/review …"`: turn the slash line into the review prompt.

    Returns None when ``message`` is not a /review line. Raises
    ``ReviewError`` (with usage) on bad input; returns "" when there is
    nothing to review (the caller prints the note and exits 0)."""
    parts = message.strip().split()
    if not parts or parts[0].lower() != "/review":
        return None
    try:
        prompt, _t, summary = build_from_args(parts[1:], cwd)
    except review.ReviewError as e:
        raise review.ReviewError(f"{e}\nUsage: {_USAGE}".strip()) from e
    print(f"/review — {summary}", file=sys.stderr)
    return prompt or ""
