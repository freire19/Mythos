"""Plan-mode and todo-list tools.

Design: stateless. Both tools encode their state into the conversation
itself — the LLM passes the latest plan/todo list every time, and the
display layer renders it. This avoids cross-turn state leakage and keeps
sessions resumable from messages alone.

`present_plan` is marked DESTRUCTIVE so the approval gate fires every time:
the user reviews the plan before any executing tool runs. The tool itself
does nothing — it's the approval prompt that gates execution.

`todo_write` is SAFE (auto-approved) — it's purely informational.
"""

from __future__ import annotations

import os
from typing import Any

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

VALID_TODO_STATUSES = ("pending", "in_progress", "completed", "cancelled")
VALID_CONFIDENCE = ("high", "medium", "low")


# ── present_plan ──


async def _present_plan(summary: str, steps: list[Any]) -> dict[str, Any]:
    if not isinstance(summary, str) or not summary.strip():
        return {"error": "summary is required"}
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a non-empty list"}

    normalized = []
    for i, s in enumerate(steps, start=1):
        text = str(s).strip()
        if not text:
            return {"error": f"step {i} is empty"}
        normalized.append(text)

    return {
        "approved": True,
        "summary": summary.strip(),
        "steps": normalized,
        "message": (
            "Plan approved by user. Proceed with execution. "
            "Do not call present_plan again unless the plan needs to change."
        ),
    }


register_tool(
    ToolDefinition(
        name="present_plan",
        description=(
            "Present a step-by-step execution plan to the user for approval BEFORE "
            "starting any non-trivial work. Call this once at the start of medium "
            "or complex tasks (3+ steps). The user must approve the plan before "
            "you run any modifying tool. After approval, follow the plan; if you "
            "deviate significantly, present_plan again."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One-sentence statement of the goal",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of concrete steps you'll take",
                },
            },
            "required": ["summary", "steps"],
        },
        safety=ToolSafety.DESTRUCTIVE,  # forces the approval gate
        executor=_present_plan,
        category=ToolCategory.PLANNING,
    )
)


# ── pre_flight ──


async def _pre_flight(
    goal: str,
    steps: list[Any],
    confidence: str,
    alternatives_rejected: list[Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Emit a strategy approval card with cost + time estimates.

    Model defaults to env-var lookup so a missing arg doesn't crash the
    estimate — the worst case is unknown-model → $0.00 surfaced as `~$?`
    on the card. Real model name comes from the agent loop wiring.
    """
    from ..config import get_provider_config
    from ..preflight import estimate_total_cost, estimate_total_time

    if not isinstance(goal, str) or not goal.strip():
        return {"error": "goal is required"}
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a non-empty list"}
    if confidence not in VALID_CONFIDENCE:
        return {
            "error": f"confidence must be one of {VALID_CONFIDENCE}, got {confidence!r}"
        }

    normalized_steps: list[dict[str, str]] = []
    for i, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            return {"error": f"step[{i}] must be an object with 'tool' and 'args_preview'"}
        tool = str(raw.get("tool", "")).strip()
        args_preview = str(raw.get("args_preview", "")).strip()
        why = str(raw.get("why", "")).strip()
        if not tool:
            return {"error": f"step[{i}] missing 'tool'"}
        normalized_steps.append(
            {"tool": tool, "args_preview": args_preview, "why": why}
        )

    normalized_alts: list[dict[str, str]] = []
    for raw in alternatives_rejected or []:
        if not isinstance(raw, dict):
            continue
        approach = str(raw.get("approach", "")).strip()
        why_rejected = str(raw.get("why_rejected", "")).strip()
        if approach:
            normalized_alts.append({"approach": approach, "why_rejected": why_rejected})

    # Best-effort model resolution. The agent loop will eventually pass
    # `model` explicitly; until then derive from the active provider so
    # estimates have a price source.
    if not model:
        provider = os.environ.get("ALPHA_PROVIDER", "deepseek")
        try:
            model = get_provider_config(provider).get("model", "")
        except Exception:
            model = ""

    estimated_cost_usd = estimate_total_cost(normalized_steps, model or "")
    estimated_time_s = estimate_total_time(normalized_steps)

    # Budget cap: per-turn USD ceiling. Unset = no cap (current behavior).
    # When the estimate exceeds the cap, surface a structured refusal
    # the agent loop can recognize and act on (slice 1 just returns the
    # error; slice 2 wires it into the loop to actually halt execution).
    cap_raw = os.environ.get("ALPHA_MAX_TURN_COST_USD", "").strip()
    if cap_raw:
        try:
            cap = float(cap_raw)
        except ValueError:
            cap = None
        if cap is not None and estimated_cost_usd > cap:
            return {
                "error": "budget_cap_exceeded",
                "estimated_cost_usd": round(estimated_cost_usd, 4),
                "cap_usd": cap,
                "message": (
                    f"Pre-flight estimated ${estimated_cost_usd:.4f} but "
                    f"ALPHA_MAX_TURN_COST_USD={cap} — turn aborted. Split "
                    f"the task, raise the cap, or remove steps."
                ),
            }

    return {
        "approved": True,
        "goal": goal.strip(),
        "steps": normalized_steps,
        "alternatives_rejected": normalized_alts,
        "confidence": confidence,
        "estimated_cost_usd": round(estimated_cost_usd, 4),
        "estimated_time_s": round(estimated_time_s, 1),
        "model": model or "",
        "message": (
            "Strategy approved by user. Execute the planned steps. "
            "Do not call pre_flight again unless the strategy changes."
        ),
    }


register_tool(
    ToolDefinition(
        name="pre_flight",
        description=(
            "Emit a structured plan card BEFORE executing a batch of tools. "
            "REQUIRED at the start of any turn that will call 2+ destructive "
            "tools OR is expected to cost more than $0.05. The user reviews "
            "the card (goal, planned tools, cost/time estimate, alternatives "
            "rejected, confidence) and approves the strategy at once instead "
            "of approving each tool individually. After approval, execute "
            "the planned steps without further per-tool prompts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "One sentence describing what this turn accomplishes",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "description": "Tool name to call"},
                            "args_preview": {
                                "type": "string",
                                "description": "Short preview of args (e.g. path, command)",
                            },
                            "why": {
                                "type": "string",
                                "description": "One-line reason this step is necessary",
                            },
                        },
                        "required": ["tool", "args_preview"],
                    },
                    "description": "Ordered list of tool calls you plan to make",
                },
                "confidence": {
                    "type": "string",
                    "enum": list(VALID_CONFIDENCE),
                    "description": "Your confidence the plan will achieve the goal",
                },
                "alternatives_rejected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "approach": {"type": "string"},
                            "why_rejected": {"type": "string"},
                        },
                        "required": ["approach"],
                    },
                    "description": "Other strategies considered and why discarded",
                },
            },
            "required": ["goal", "steps", "confidence"],
        },
        safety=ToolSafety.DESTRUCTIVE,  # forces the approval gate
        executor=_pre_flight,
        category=ToolCategory.PLANNING,
    )
)


# ── todo_write ──


async def _todo_write(todos: list[Any]) -> dict[str, Any]:
    if not isinstance(todos, list):
        return {"error": "todos must be a list"}

    cleaned = []
    seen_in_progress = 0
    for i, raw in enumerate(todos):
        if not isinstance(raw, dict):
            return {"error": f"todo[{i}] must be an object with 'content' and 'status'"}
        content = str(raw.get("content", "")).strip()
        status = str(raw.get("status", "pending")).strip()
        if not content:
            return {"error": f"todo[{i}] missing 'content'"}
        if status not in VALID_TODO_STATUSES:
            return {
                "error": (
                    f"todo[{i}] has invalid status '{status}'. "
                    f"Must be one of {VALID_TODO_STATUSES}"
                )
            }
        if status == "in_progress":
            seen_in_progress += 1
        cleaned.append({"content": content, "status": status})

    warning = None
    if seen_in_progress > 1:
        warning = (
            f"{seen_in_progress} todos are 'in_progress'. "
            "Prefer keeping exactly one in progress at a time."
        )

    counts = {s: 0 for s in VALID_TODO_STATUSES}
    for t in cleaned:
        counts[t["status"]] += 1

    result: dict[str, Any] = {
        "ok": True,
        "todos": cleaned,
        "counts": counts,
    }
    if warning:
        result["warning"] = warning
    return result


register_tool(
    ToolDefinition(
        name="todo_write",
        description=(
            "Maintain a checklist of subtasks for the current request. Pass the "
            "ENTIRE list every time — this tool replaces, not appends. Use it for "
            "tasks with 3+ distinct steps. Mark exactly one item 'in_progress' "
            "while you're working on it; flip to 'completed' as soon as it's "
            "done. Skip this for trivial single-step requests."
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Imperative-form description of the subtask",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(VALID_TODO_STATUSES),
                            },
                        },
                        "required": ["content", "status"],
                    },
                    "description": "Full replacement list of all current todos",
                }
            },
            "required": ["todos"],
        },
        safety=ToolSafety.SAFE,
        executor=_todo_write,
        category=ToolCategory.PLANNING,
    )
)
