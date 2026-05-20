"""Skill registry — discovers SKILL.md files in known locations.

Backed por `alpha._registry.FileBackedRegistry` para evitar duplicacao
com agents/registry.py (#DM008).
"""

from __future__ import annotations

import logging
from pathlib import Path

from .._registry import FileBackedRegistry
from ..config import _PROJECT_ROOT
from .loader import Skill, load_skill_file

logger = logging.getLogger(__name__)

_SEARCH_PATHS = [
    Path.home() / ".alpha" / "skills",
    _PROJECT_ROOT / "skills",
]

_registry: FileBackedRegistry[Skill] = FileBackedRegistry(
    _SEARCH_PATHS, "*/SKILL.md", load_skill_file, kind="skill"
)


def load_all_skills(force: bool = False) -> dict[str, Skill]:
    result = _registry.load_all(force=force)
    # DEEP_PERFORMANCE #D029: invalidar cache do _SlashCompleter quando
    # skills são recarregadas (startup ou /reload).
    try:
        from ..repl_input import _SlashCompleter
        _SlashCompleter.invalidate_cache()
    except Exception as e:
        # Import circular ou repl_input nao carregado (e.g. modo daemon
        # sem REPL) — debug log preserva o diagnose path.
        logger.debug("SlashCompleter cache invalidate skipped: %s", e)
    return result


def get_skill(name: str) -> Skill | None:
    return _registry.get(name)


def list_skills() -> list[Skill]:
    return _registry.list()
