"""Tests for `alpha.skills.manager` (Plano-Upgrade-v3 H3 #16).

Git installs are exercised by initializing a real local repo in a temp
dir and pointing the installer at its file:// URL — no network, no
mocked subprocess. That covers the clone path end-to-end including
`_git_commit_short` and `_find_skill_dirs`.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from alpha.skills import manager


# ─── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def user_skills_dir(tmp_path, monkeypatch):
    """Redirect USER_SKILLS_DIR + index to a tmp location for each test.

    We patch both module-level constants so the test can't accidentally
    write to the real `~/.alpha/skills/` even if a code path forgets to
    go through a helper.
    """
    skills_dir = tmp_path / "user_skills"
    monkeypatch.setattr(manager, "USER_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(manager, "INSTALLED_INDEX", skills_dir / ".installed.json")
    return skills_dir


def _write_skill(target: Path, name: str, description: str = "test skill") -> Path:
    """Create a minimal valid SKILL.md at `target/`."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: {description}
            ---

            # {name}

            Body of the skill.
            """
        ),
        encoding="utf-8",
    )
    return target


def _init_git_repo(repo_dir: Path) -> None:
    """Initialize a git repo with one commit. Skips the test if git is unavailable."""
    try:
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True, timeout=10)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."],
            cwd=repo_dir,
            check=True,
            timeout=10,
        )
        subprocess.run(
            [
                "git",
                "-c", "user.email=t@t",
                "-c", "user.name=t",
                "commit",
                "-q",
                "-m",
                "init",
            ],
            cwd=repo_dir,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        pytest.skip(f"git not available: {e}")


# ─── parse_source ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source,kind,expected_contains",
    [
        ("github:user/repo", "git", "https://github.com/user/repo.git"),
        ("git+https://example.com/r.git", "git", "https://example.com/r.git"),
        ("https://github.com/user/repo.git", "git", "github.com/user/repo"),
        ("git@github.com:user/repo.git", "git", "user/repo"),
        ("/tmp/local/skill", "path", "/tmp/local/skill"),
    ],
)
def test_parse_source_kinds(source, kind, expected_contains):
    parsed = manager.parse_source(source)
    assert parsed.kind == kind
    assert expected_contains in parsed.normalized
    assert parsed.original == source


def test_parse_source_rejects_empty():
    with pytest.raises(manager.SkillInstallError):
        manager.parse_source("")


def test_parse_source_rejects_bad_github_shorthand():
    with pytest.raises(manager.SkillInstallError, match="github shorthand"):
        manager.parse_source("github:foo")  # missing /repo


# ─── install from local path ─────────────────────────────────────


def test_install_single_skill_from_path(tmp_path, user_skills_dir):
    src = _write_skill(tmp_path / "src" / "myskill", "myskill")

    result = manager.install(str(src))

    assert result.installed == ["myskill"]
    assert (user_skills_dir / "myskill" / "SKILL.md").is_file()

    # Index records source + kind.
    index = json.loads((user_skills_dir / ".installed.json").read_text())
    assert index["myskill"]["kind"] == "path"
    assert "installed_at" in index["myskill"]


def test_install_multi_skill_repo_from_path(tmp_path, user_skills_dir):
    repo = tmp_path / "multi-repo"
    _write_skill(repo / "skills" / "alpha", "alpha")
    _write_skill(repo / "skills" / "beta", "beta")

    result = manager.install(str(repo))

    assert sorted(result.installed) == ["alpha", "beta"]
    assert (user_skills_dir / "alpha" / "SKILL.md").is_file()
    assert (user_skills_dir / "beta" / "SKILL.md").is_file()


def test_install_skips_existing_without_force(tmp_path, user_skills_dir):
    src = _write_skill(tmp_path / "src" / "myskill", "myskill")
    manager.install(str(src))
    result = manager.install(str(src))

    assert result.installed == []
    assert result.skipped and result.skipped[0][0] == "myskill"


def test_install_force_overwrites(tmp_path, user_skills_dir):
    src = _write_skill(tmp_path / "src" / "myskill", "myskill")
    manager.install(str(src))

    # Modify source, re-install with force.
    (src / "marker.txt").write_text("v2", encoding="utf-8")
    result = manager.install(str(src), force=True)

    assert result.installed == ["myskill"]
    assert (user_skills_dir / "myskill" / "marker.txt").read_text() == "v2"


def test_install_name_override_renames_dest(tmp_path, user_skills_dir):
    src = _write_skill(tmp_path / "src" / "original", "original")
    result = manager.install(str(src), name_override="renamed")
    assert result.installed == ["renamed"]
    assert (user_skills_dir / "renamed").is_dir()
    assert not (user_skills_dir / "original").exists()


def test_install_name_override_rejected_for_multi_skill(tmp_path, user_skills_dir):
    repo = tmp_path / "multi"
    _write_skill(repo / "skills" / "a", "a")
    _write_skill(repo / "skills" / "b", "b")
    with pytest.raises(manager.SkillInstallError, match="single-skill"):
        manager.install(str(repo), name_override="renamed")


def test_install_missing_skill_md_errors(tmp_path, user_skills_dir):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(manager.SkillInstallError, match="no SKILL.md found"):
        manager.install(str(empty))


def test_install_path_not_found(user_skills_dir):
    with pytest.raises(manager.SkillInstallError, match="not found"):
        manager.install("/definitely/not/a/real/path/xyz")


# ─── install from git ────────────────────────────────────────────


def test_install_from_git_file_url(tmp_path, user_skills_dir):
    """End-to-end git clone using a local file:// URL.

    file:// URLs hit the same `git clone` code path as HTTPS but don't
    need the network, which keeps the test hermetic.
    """
    repo_dir = tmp_path / "remote-repo"
    _write_skill(repo_dir / "myskill", "myskill")
    _init_git_repo(repo_dir)

    result = manager.install(f"file://{repo_dir}")

    assert result.installed == ["myskill"]
    assert (user_skills_dir / "myskill" / "SKILL.md").is_file()

    index = json.loads((user_skills_dir / ".installed.json").read_text())
    assert index["myskill"]["kind"] == "git"
    # commit hash is short (7+ chars typically) — just check it's recorded.
    assert index["myskill"].get("commit"), "commit hash should be recorded"


# ─── list / remove / update ──────────────────────────────────────


def test_list_empty(user_skills_dir):
    assert manager.list_installed() == []


def test_list_includes_untracked_skills(tmp_path, user_skills_dir):
    # Tracked install
    src = _write_skill(tmp_path / "tracked", "tracked")
    manager.install(str(src))

    # Skill dropped by hand (no index entry)
    _write_skill(user_skills_dir / "manual", "manual")

    rows = {r["name"]: r for r in manager.list_installed()}
    assert rows["tracked"]["tracked"] is True
    assert rows["manual"]["tracked"] is False
    assert rows["manual"]["source"] == "local-untracked"


def test_remove_deletes_dir_and_index(tmp_path, user_skills_dir):
    src = _write_skill(tmp_path / "myskill", "myskill")
    manager.install(str(src))

    assert manager.remove("myskill") is True
    assert not (user_skills_dir / "myskill").exists()
    index = json.loads((user_skills_dir / ".installed.json").read_text())
    assert "myskill" not in index


def test_remove_returns_false_for_unknown(user_skills_dir):
    assert manager.remove("nonexistent") is False


def test_update_reinstalls_from_recorded_source(tmp_path, user_skills_dir):
    src = _write_skill(tmp_path / "myskill", "myskill")
    manager.install(str(src))

    # Mutate source — update should pick the new content up.
    (src / "marker.txt").write_text("updated", encoding="utf-8")
    result = manager.update("myskill")

    assert "myskill" in result.installed
    assert (user_skills_dir / "myskill" / "marker.txt").read_text() == "updated"


def test_update_unknown_name_errors(user_skills_dir):
    with pytest.raises(manager.SkillInstallError, match="not tracked"):
        manager.update("never-installed")


def test_update_with_empty_index_errors(user_skills_dir):
    with pytest.raises(manager.SkillInstallError, match="no tracked"):
        manager.update()


def test_update_all_iterates_every_tracked(tmp_path, user_skills_dir):
    a = _write_skill(tmp_path / "src" / "a", "a")
    b = _write_skill(tmp_path / "src" / "b", "b")
    manager.install(str(a))
    manager.install(str(b))

    (a / "v.txt").write_text("a2", encoding="utf-8")
    (b / "v.txt").write_text("b2", encoding="utf-8")

    result = manager.update()
    assert set(result.installed) == {"a", "b"}
    assert (user_skills_dir / "a" / "v.txt").read_text() == "a2"
    assert (user_skills_dir / "b" / "v.txt").read_text() == "b2"


# ─── index resilience ────────────────────────────────────────────


def test_corrupt_index_does_not_block_install(tmp_path, user_skills_dir):
    user_skills_dir.mkdir(parents=True, exist_ok=True)
    (user_skills_dir / ".installed.json").write_text("{not json", encoding="utf-8")

    src = _write_skill(tmp_path / "myskill", "myskill")
    # _load_index() should swallow the JSONDecodeError and start fresh.
    result = manager.install(str(src))

    assert result.installed == ["myskill"]
    # Index is now valid JSON again.
    json.loads((user_skills_dir / ".installed.json").read_text())
