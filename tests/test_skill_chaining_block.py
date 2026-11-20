"""Hard block on autonomous skill-chaining via the load_skill tool.

A single `/audit-1-setup` was turning into a full audit→scan→review→report→
apply-fixes run because the model autonomously called `load_skill` to chain the
next pipeline skill. Prose `PARE AQUI` in the skills did not bind the model;
this harness block does. See alpha/tools/skill_tools.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from alpha.skills.loader import Skill
from alpha.tools.skill_tools import (
    _load_skill,
    clear_active_skill,
    get_active_skill,
    set_active_skill,
)


def _skill(name: str, orchestrator: bool = False) -> Skill:
    md = {"alpha": {"orchestrator": True}} if orchestrator else {}
    return Skill(
        name=name, description="d", body="BODY", path=Path("/tmp/x"),
        metadata=md, requires_bins=[],
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Hermetic skills (no disk) + clean active-skill + no env override."""
    skills = {
        "audit-2-scan": _skill("audit-2-scan"),
        "audit-3-review": _skill("audit-3-review"),
        "front-deep": _skill("front-deep", orchestrator=True),
        "sub": _skill("sub"),
    }
    monkeypatch.setattr("alpha.skills.get_skill", lambda n: skills.get(n))
    monkeypatch.setattr("alpha.skills.list_skills", lambda: list(skills.values()))
    monkeypatch.delenv("ALPHA_ALLOW_SKILL_CHAINING", raising=False)
    clear_active_skill()
    yield
    clear_active_skill()


def test_blocks_chaining_to_a_different_skill():
    set_active_skill("audit-2-scan")
    res = asyncio.run(_load_skill("audit-3-review"))
    assert res.get("ok") is False
    assert res.get("error") == "skill_chaining_blocked", res
    assert "audit-3-review" in res["message"]


def test_first_load_pins_and_allows():
    res = asyncio.run(_load_skill("audit-2-scan"))
    assert "instructions" in res
    assert get_active_skill() == "audit-2-scan"


def test_reloading_same_active_skill_is_allowed():
    set_active_skill("audit-2-scan")
    res = asyncio.run(_load_skill("audit-2-scan"))
    assert "instructions" in res


def test_orchestrator_may_chain_subskills():
    set_active_skill("front-deep")  # orchestrator
    res = asyncio.run(_load_skill("sub"))
    assert "instructions" in res, res
    # Orchestrator stays in charge — sub-skill does not become active.
    assert get_active_skill() == "front-deep"


def test_env_override_disables_the_block(monkeypatch):
    monkeypatch.setenv("ALPHA_ALLOW_SKILL_CHAINING", "1")
    set_active_skill("audit-2-scan")
    res = asyncio.run(_load_skill("audit-3-review"))
    assert "instructions" in res, res
