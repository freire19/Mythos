"""Skill installation manager (Plano-Upgrade-v3 H3 #16).

Installs skills from git URLs or local paths into `~/.alpha/skills/`,
where the existing `registry.py` will auto-discover them on next launch.

A skill "source" can be:

  - A git URL: `https://github.com/user/repo.git`, `git@github.com:...`,
    or `git+https://...` (the `git+` prefix is stripped for compatibility
    with pip-style URLs).
  - A GitHub shorthand: `github:user/repo` (expanded to the HTTPS URL).
  - A local filesystem path: anything that resolves to an existing
    directory containing a `SKILL.md` (single-skill repo) or
    `skills/*/SKILL.md` (multi-skill repo).

The installer writes a JSON index at `~/.alpha/skills/.installed.json`
so `alpha skills list` can show provenance and `alpha skills update`
knows how to re-fetch each entry. The index is the source of truth
for "managed by alpha"; skills dropped into `~/.alpha/skills/` by hand
still work for discovery but are flagged as `source: "local-untracked"`
in `list`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

USER_SKILLS_DIR = Path.home() / ".alpha" / "skills"
INSTALLED_INDEX = USER_SKILLS_DIR / ".installed.json"


SourceKind = Literal["git", "path"]


@dataclass
class ParsedSource:
    kind: SourceKind
    normalized: str
    original: str


@dataclass
class InstallResult:
    installed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    errors: list[str] = field(default_factory=list)


class SkillInstallError(RuntimeError):
    """Raised for unrecoverable install errors the CLI should surface."""


def parse_source(source: str) -> ParsedSource:
    """Classify a user-supplied source string.

    Accepts git URLs (with or without `git+` prefix), `github:user/repo`
    shorthand, and local paths. Raises `SkillInstallError` for empty
    or obviously malformed input.
    """
    s = source.strip()
    if not s:
        raise SkillInstallError("source cannot be empty")

    if s.startswith("git+"):
        return ParsedSource("git", s[4:], s)
    if s.startswith("github:"):
        spec = s[len("github:") :]
        if "/" not in spec:
            raise SkillInstallError(
                f"invalid github shorthand: {s!r} (expected github:user/repo)"
            )
        return ParsedSource("git", f"https://github.com/{spec}.git", s)
    # file:// is treated as git unconditionally — it's only meaningful
    # as a clone target (test harnesses use it heavily, and there's no
    # plain-file-copy CLI affordance that takes file:// URLs).
    if s.startswith("file://"):
        return ParsedSource("git", s, s)
    if s.startswith(("https://", "http://", "git@", "ssh://")) and (
        s.endswith(".git") or "github.com" in s or "gitlab" in s or "bitbucket" in s
    ):
        return ParsedSource("git", s, s)

    # Anything else is treated as a local path. We don't existence-check
    # here — the caller (_install_from_path) will, with a clearer error.
    return ParsedSource("path", str(Path(s).expanduser()), s)


def _load_index() -> dict[str, dict]:
    if not INSTALLED_INDEX.exists():
        return {}
    try:
        return json.loads(INSTALLED_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt index — preserve the bad file for forensics, start fresh.
        # The user can still `alpha skills install` again; the on-disk
        # skill dirs survive whether the index is valid or not.
        return {}


def _save_index(index: dict[str, dict]) -> None:
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLED_INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(INSTALLED_INDEX)


def _git_commit_short(repo_dir: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _find_skill_dirs(root: Path) -> list[Path]:
    """Return directories under `root` that contain a SKILL.md.

    Two layouts are supported:
      1. `root/SKILL.md`              → single-skill repo (returns [root])
      2. `root/<name>/SKILL.md`       → multi-skill repo (returns each subdir)
      3. `root/skills/<name>/SKILL.md` → conventional "skills" directory layout

    We don't descend further to keep the contract obvious and avoid
    picking up SKILL.md files inside docs/test fixtures.
    """
    if (root / "SKILL.md").is_file():
        return [root]

    found: list[Path] = []
    for parent in (root, root / "skills"):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.append(child)
        if found:
            return found
    return found


def _copy_skill(src_dir: Path, dest_dir: Path, *, force: bool) -> tuple[str, bool]:
    """Copy a single skill directory into the user skills dir.

    Returns (name, installed_ok). If the destination exists and
    `force=False`, returns (name, False) and the caller records a skip.
    """
    name = dest_dir.name
    if dest_dir.exists():
        if not force:
            return (name, False)
        shutil.rmtree(dest_dir)
    shutil.copytree(src_dir, dest_dir)
    return (name, True)


def install(
    source: str,
    *,
    name_override: str | None = None,
    force: bool = False,
) -> InstallResult:
    parsed = parse_source(source)
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    if parsed.kind == "git":
        return _install_from_git(parsed, name_override=name_override, force=force)
    return _install_from_path(parsed, name_override=name_override, force=force)


def _install_from_git(
    parsed: ParsedSource,
    *,
    name_override: str | None,
    force: bool,
) -> InstallResult:
    result = InstallResult()
    with tempfile.TemporaryDirectory(prefix="alpha-skill-") as tmp:
        clone_dir = Path(tmp) / "clone"
        try:
            subprocess.run(
                # --depth 1 keeps the clone fast for the common case.
                # `alpha skills update` re-clones rather than fetching,
                # so shallow history is fine permanently.
                ["git", "clone", "--depth", "1", parsed.normalized, str(clone_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip().splitlines()[-3:]
            raise SkillInstallError(
                f"git clone failed for {parsed.original}: {' | '.join(stderr)}"
            ) from e
        except FileNotFoundError as e:
            raise SkillInstallError("git executable not found in PATH") from e
        except subprocess.TimeoutExpired as e:
            raise SkillInstallError(
                f"git clone timed out (>120s) for {parsed.original}"
            ) from e

        commit = _git_commit_short(clone_dir)
        skill_dirs = _find_skill_dirs(clone_dir)
        if not skill_dirs:
            raise SkillInstallError(
                f"no SKILL.md found in {parsed.original} "
                "(expected SKILL.md at repo root, in subdirs, or under skills/)"
            )

        _stage_skills(
            skill_dirs,
            result,
            source=parsed.original,
            kind="git",
            name_override=name_override,
            commit=commit,
            force=force,
        )
    return result


def _install_from_path(
    parsed: ParsedSource,
    *,
    name_override: str | None,
    force: bool,
) -> InstallResult:
    path = Path(parsed.normalized).resolve()
    if not path.is_dir():
        raise SkillInstallError(f"local path not found or not a directory: {path}")

    skill_dirs = _find_skill_dirs(path)
    if not skill_dirs:
        raise SkillInstallError(
            f"no SKILL.md found in {path} "
            "(expected SKILL.md at root, in subdirs, or under skills/)"
        )

    result = InstallResult()
    _stage_skills(
        skill_dirs,
        result,
        source=str(path),
        kind="path",
        name_override=name_override,
        commit="",
        force=force,
    )
    return result


def _stage_skills(
    skill_dirs: list[Path],
    result: InstallResult,
    *,
    source: str,
    kind: SourceKind,
    name_override: str | None,
    commit: str,
    force: bool,
) -> None:
    """Copy each SKILL.md dir into USER_SKILLS_DIR and update the index."""
    if name_override and len(skill_dirs) > 1:
        raise SkillInstallError(
            "--name can only override a single-skill source; "
            f"{source} contains {len(skill_dirs)} skills"
        )

    index = _load_index()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for src in skill_dirs:
        dest_name = name_override or src.name
        dest = USER_SKILLS_DIR / dest_name
        name, ok = _copy_skill(src, dest, force=force)
        if not ok:
            result.skipped.append(
                (name, "already installed; pass --force to overwrite")
            )
            continue

        entry: dict = {
            "source": source,
            "kind": kind,
            "installed_at": now,
        }
        if commit:
            entry["commit"] = commit
        index[name] = entry
        result.installed.append(name)

    _save_index(index)


def remove(name: str) -> bool:
    """Delete a managed skill. Returns True if anything was removed.

    Removes both the directory and the index entry, independently —
    so a half-installed skill (dir without index entry, or vice versa)
    still gets cleaned up by this call.
    """
    removed = False
    target = USER_SKILLS_DIR / name
    if target.exists():
        shutil.rmtree(target)
        removed = True

    index = _load_index()
    if name in index:
        del index[name]
        _save_index(index)
        removed = True

    return removed


def list_installed() -> list[dict]:
    """Return entries for every skill under `~/.alpha/skills/`.

    Skills present on disk but missing from the index are returned with
    `source: "local-untracked"` so the user knows they exist but aren't
    managed by `alpha skills install`/`update`.
    """
    if not USER_SKILLS_DIR.is_dir():
        return []

    index = _load_index()
    on_disk = sorted(
        d.name for d in USER_SKILLS_DIR.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file() and not d.name.startswith(".")
    )

    rows: list[dict] = []
    for name in on_disk:
        entry = index.get(name)
        if entry:
            rows.append({"name": name, **entry, "tracked": True})
        else:
            rows.append({
                "name": name,
                "source": "local-untracked",
                "kind": "path",
                "installed_at": "",
                "tracked": False,
            })
    return rows


def update(name: str | None = None) -> InstallResult:
    """Re-install one (or all) tracked skill(s) from their original source.

    Local-untracked skills are skipped — there's no recorded source to
    pull from. Path-installed skills re-copy from the original location;
    git-installed skills re-clone (`--depth 1` so this stays fast).
    """
    index = _load_index()

    targets: list[str]
    if name is None:
        if not index:
            raise SkillInstallError("no tracked skills to update")
        targets = sorted(index.keys())
    elif name in index:
        targets = [name]
    else:
        raise SkillInstallError(f"skill not tracked: {name!r} (run `alpha skills list`)")

    combined = InstallResult()
    for skill_name in targets:
        entry = index[skill_name]
        source = entry["source"]
        try:
            sub = install(source, force=True)
        except SkillInstallError as e:
            combined.errors.append(f"{skill_name}: {e}")
            continue
        combined.installed.extend(sub.installed)
        combined.skipped.extend(sub.skipped)

    return combined
