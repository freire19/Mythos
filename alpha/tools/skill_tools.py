"""Skill loader tool — exposes load_skill(name) to the agent."""

from __future__ import annotations

import logging
import os
import shutil

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)


# ── Active-skill tracking (hard block on autonomous skill-chaining) ──
# Set when the user invokes `/<skill>` (main._try_skill_dispatch) or when the
# model loads the first skill of a task. While set, the model CANNOT load a
# DIFFERENT skill via this tool — that hard-blocks the autonomous chaining
# (audit-1 → 2 → 3 → 4 → apply-fixes) that turned a single `/audit-1-setup` into
# a full audit + code-modification run. Prose `PARE AQUI` in the skills did NOT
# bind the model (DeepSeek); this does. Cleared on each fresh interactive prompt
# (main.run_repl), so the USER drives the pipeline step by step via slash, while
# auto-continues (`[CONTINUE:]`) preserve the active skill across turns.
# Override with ALPHA_ALLOW_SKILL_CHAINING=1; orchestrator skills (frontmatter
# metadata.alpha.orchestrator: true) are exempt so they can drive sub-skills.
_active_skill: str | None = None


def set_active_skill(name: str | None) -> None:
    global _active_skill
    _active_skill = (name or "").strip() or None


def clear_active_skill() -> None:
    global _active_skill
    _active_skill = None


def get_active_skill() -> str | None:
    return _active_skill


async def _load_skill(name: str) -> dict:
    """Return the full body of a named skill plus bin availability info."""
    from ..skills import get_skill, list_skills

    target = (name or "").strip()
    if not target:
        return {"error": "Missing skill name", "available": [s.name for s in list_skills()]}

    skill = get_skill(target)
    if not skill:
        return {
            "error": f"Skill '{target}' not found",
            "available": [s.name for s in list_skills()],
        }

    # Hard block on autonomous skill-chaining (see _active_skill note above).
    active = _active_skill
    if (
        active
        and active != skill.name
        and not os.environ.get("ALPHA_ALLOW_SKILL_CHAINING")
    ):
        active_obj = get_skill(active)
        alpha_meta = active_obj.metadata.get("alpha") if active_obj else None
        is_orchestrator = bool(
            isinstance(alpha_meta, dict) and alpha_meta.get("orchestrator")
        )
        if not is_orchestrator:
            logger.info(
                "Blocked autonomous load_skill(%s) while '%s' is active", skill.name, active
            )
            return {
                "ok": False,
                "error": "skill_chaining_blocked",
                "message": (
                    f"A skill '{active}' já está ativa nesta tarefa. NÃO carregue "
                    f"'{skill.name}' agora: conclua a tarefa atual, escreva seu resumo "
                    f"e devolva o controle ao usuário — é ele quem invoca a próxima "
                    f"etapa com /{skill.name} quando quiser. (Encadeamento automático "
                    f"de skills está desativado para evitar tarefas que nunca terminam "
                    f"e mudanças não solicitadas.)"
                ),
            }

    # Pin the first skill of a task as active (no-op if a slash invoke already
    # did, or if an orchestrator is loading a sub-skill — it stays in charge).
    if not active:
        set_active_skill(skill.name)

    missing = [b for b in skill.requires_bins if not shutil.which(b)]
    result: dict = {
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.body,
    }
    if missing:
        result["missing_binaries"] = missing
        result["warning"] = (
            f"Skill requires binaries not found on PATH: {missing}. "
            "Install them before proceeding, or choose another approach."
        )
    return result


_LOAD_SKILL_BASE_DESC = (
    "Load the full instructions for a named skill. "
    "Call this BEFORE performing a task when a matching skill exists — "
    "it contains critical guidance, commands, and gotchas. "
    "Call with no name to receive the available list."
)


def _build_load_skill_description(names: list[str] | None = None) -> str:
    """Build description com a lista de skills disponiveis (#D017-PERF).

    DEEP_PERFORMANCE #DP025: ja NAO chama `load_all_skills()`. Antes, montar
    a description no import do modulo (parte de `load_all_tools()`) disparava
    um walk no disco de todos os `*/SKILL.md` em TODO startup — inclusive
    single-shot que nunca usa skill. Agora os nomes sao injetados em setup-time
    via `refresh_load_skill_description()`, depois que `build_system_prompt`
    ja carregou as skills. Importar este modulo virou efeito-zero no disco."""
    base = _LOAD_SKILL_BASE_DESC
    if names:
        base += f" Available skills: {', '.join(sorted(names))}."
    return base


def refresh_load_skill_description(name_filter=None) -> None:
    """Inject the loaded skill names into the registered `load_skill` tool's
    description. Idempotent; call at setup after skills are loaded.

    Mutates `ToolDefinition.description` in place — `get_openai_tools` reads it
    fresh on every call (no cache), so a refresh before serialization is enough
    for the names to reach the LLM. The `#D017-PERF` contract still holds: skill
    names live in THIS description, not the system prompt."""
    from . import get_tool

    td = get_tool("load_skill")
    if td is None:
        return
    try:
        from ..skills import load_all_skills
        from ..skills.prompt import list_skill_names_for_tool_description

        load_all_skills()  # cached no-op if build_system_prompt already loaded
        names = list_skill_names_for_tool_description(name_filter)
    except Exception:
        names = []
    td.description = _build_load_skill_description(names)


register_tool(
    ToolDefinition(
        name="load_skill",
        description=_build_load_skill_description(),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The exact skill name (see the Available skills list in this tool's description).",
                },
            },
            "required": ["name"],
        },
        safety=ToolSafety.SAFE,
        executor=_load_skill,
        category=ToolCategory.SKILLS,
    )
)
