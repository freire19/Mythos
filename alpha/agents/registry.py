"""Agent registry — discovers agent.yaml files in known locations.

Backed por `alpha._registry.FileBackedRegistry` para evitar duplicacao
com skills/registry.py (#DM008).
"""

from __future__ import annotations

from pathlib import Path

from .._registry import FileBackedRegistry
from .._resources import package_data
from ..config import _PROJECT_ROOT  # #095: fonte unica
from .loader import load_agent_file
from .scope import AgentScope

# Mythos's FileBackedRegistry is FIRST-wins (see _registry.py:57 — skips
# names already loaded from an earlier path), so the order is user >
# project > bundled fallback. Bundled lives last so users can override
# default/lean/researcher by dropping a same-named agent.yaml under
# ~/.alpha/agents/ or <project>/agents/. `package_data` resolves the
# bundled dir whether the install is editable, wheel, or zipped.
_SEARCH_PATHS = [
    Path.home() / ".alpha" / "agents",
    _PROJECT_ROOT / "agents",
    package_data("data", "agents"),
]

_registry: FileBackedRegistry[AgentScope] = FileBackedRegistry(
    _SEARCH_PATHS, "*/agent.yaml", load_agent_file, kind="agent"
)


def load_all_agents(force: bool = False) -> dict[str, AgentScope]:
    return _registry.load_all(force=force)


def get_agent(name: str) -> AgentScope | None:
    return _registry.get(name)


def list_agents() -> list[AgentScope]:
    return _registry.list()
