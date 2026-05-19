"""Agent registry — discovers agent.yaml files in known locations.

Backed por `alpha._registry.FileBackedRegistry` para evitar duplicacao
com skills/registry.py (#DM008).
"""

from __future__ import annotations

from pathlib import Path

from .._registry import FileBackedRegistry
from ..config import _PROJECT_ROOT  # #095: fonte unica
from .loader import load_agent_file
from .scope import AgentScope

# Search order: bundled defaults → user-global → project-local override.
# FileBackedRegistry indexes by name and the LAST source wins, so the
# ordering below means project files override user files, which override
# the bundled defaults. The bundled `alpha/data/agents/` ships inside
# the wheel so pipx users get default/lean/researcher without copying
# them by hand. For `pip install -e .` dev checkouts the project-local
# `_PROJECT_ROOT/agents` is empty (the YAMLs were moved into the package).
_BUNDLED_AGENTS_DIR = Path(__file__).resolve().parent.parent / "data" / "agents"

_SEARCH_PATHS = [
    _BUNDLED_AGENTS_DIR,
    Path.home() / ".alpha" / "agents",
    _PROJECT_ROOT / "agents",
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
