"""Regression tests for the DEEP_BUGS wave-3 fixes (delegate + skill tools).

- #DB25: `_prepare_subagent_run` must not leak an empty scratch dir when an
  agent profile filters all tools (early return) or when prep raises after
  the scratch dir was created.
- #DB27: `_load_skill` must not crash with AttributeError when the active
  skill's `metadata.alpha` is a YAML scalar (str/list) instead of a mapping.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from alpha.tools import delegate_tools
from alpha.tools import skill_tools
from alpha.skills.loader import Skill


# ── #DB25 ────────────────────────────────────────────────────────────────


def _profile_scope(filter_result):
    return types.SimpleNamespace(
        provider="",
        temperature=None,
        workspace=None,
        system_prompt_extra="",
        tools_allow=["only_this"],
        tools_deny=[],
        filter_tools=lambda names: filter_result,
    )


class TestDB25ScratchDirLeak:
    def _runs_dir(self, tmp_path: Path) -> Path:
        return tmp_path / ".alpha" / "runs"

    def test_profile_filtering_all_tools_removes_scratch(self, tmp_path):
        scope = _profile_scope(filter_result=[])
        prep, err = delegate_tools._prepare_subagent_run(
            task="t",
            context="",
            tools_filter="",
            provider="",
            agent="prof",
            scope=scope,
            parent_approval_callback=None,
            parent_workspace=str(tmp_path),
        )
        assert prep is None
        assert err is not None and err["ok"] is False
        assert "filtra todas as tools" in err["error"]
        runs = self._runs_dir(tmp_path)
        assert not runs.exists() or not any(runs.iterdir())

    def test_raise_during_prep_removes_scratch(self, tmp_path, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("prep quebrou")

        monkeypatch.setattr(delegate_tools, "_build_subagent_messages", boom)
        with pytest.raises(RuntimeError, match="prep quebrou"):
            delegate_tools._prepare_subagent_run(
                task="t",
                context="",
                tools_filter="",
                provider="",
                agent="",
                scope=None,
                parent_approval_callback=None,
                parent_workspace=str(tmp_path),
            )
        runs = self._runs_dir(tmp_path)
        assert not runs.exists() or not any(runs.iterdir())

    def test_successful_prep_keeps_scratch(self, tmp_path):
        prep, err = delegate_tools._prepare_subagent_run(
            task="t",
            context="",
            tools_filter="",
            provider="",
            agent="",
            scope=None,
            parent_approval_callback=None,
            parent_workspace=str(tmp_path),
        )
        assert err is None
        assert prep.scratch_dir.exists()


# ── #DB27 ────────────────────────────────────────────────────────────────


def _make_skill(name: str, metadata: dict | None = None) -> Skill:
    return Skill(
        name=name,
        description="d",
        body="corpo",
        path=Path("/nonexistent/SKILL.md"),
        metadata=metadata or {},
    )


@pytest.fixture(autouse=True)
def _reset_active_skill(monkeypatch):
    monkeypatch.delenv("ALPHA_ALLOW_SKILL_CHAINING", raising=False)
    skill_tools.clear_active_skill()
    yield
    skill_tools.clear_active_skill()


class TestDB27ScalarAlphaMetadata:
    def _patch_registry(self, monkeypatch, skills: dict[str, Skill]):
        import alpha.skills as skills_mod

        monkeypatch.setattr(skills_mod, "get_skill", lambda n: skills.get(n))
        monkeypatch.setattr(
            skills_mod, "list_skills", lambda: list(skills.values())
        )

    @pytest.mark.parametrize("alpha_meta", ["true", ["orchestrator"], 1])
    async def test_scalar_alpha_blocks_without_crash(
        self, monkeypatch, alpha_meta
    ):
        skills = {
            "ativa": _make_skill("ativa", {"alpha": alpha_meta}),
            "outra": _make_skill("outra"),
        }
        self._patch_registry(monkeypatch, skills)
        skill_tools.set_active_skill("ativa")

        result = await skill_tools._load_skill("outra")
        assert result["ok"] is False
        assert result["error"] == "skill_chaining_blocked"

    async def test_orchestrator_dict_still_exempt(self, monkeypatch):
        skills = {
            "ativa": _make_skill("ativa", {"alpha": {"orchestrator": True}}),
            "outra": _make_skill("outra"),
        }
        self._patch_registry(monkeypatch, skills)
        skill_tools.set_active_skill("ativa")

        result = await skill_tools._load_skill("outra")
        assert result.get("error") != "skill_chaining_blocked"
        assert result.get("name") == "outra"
