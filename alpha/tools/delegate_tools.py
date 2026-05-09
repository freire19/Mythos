"""Delegate tools — spawn sub-agents to handle tasks independently.

Supports single delegation (delegate_task) and parallel delegation
(delegate_parallel) with concurrency limited by max_parallel_agents.

Split into focused modules (#082):
- _delegate_policy.py  — blocklist, prompt loading, approval gate
- _delegate_scratch.py — agent IDs, scratch dir creation, snapshots
"""

import asyncio
import json
import logging
import sys

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ..config import FEATURES
from ..display import print_subagent_event
from ._delegate_policy import (
    SUBAGENT_DESTRUCTIVE_BLOCKLIST,
    _auto_approve_no_callback,
    _load_subagent_prompt,
)
from ._delegate_scratch import _create_scratch_dir, _new_agent_id, _snapshot_dir
from .workspace import AGENT_WORKSPACE

logger = logging.getLogger(__name__)


async def _run_subagent(
    task: str,
    context: str = "",
    tools_filter: str = "",
    provider: str = "",
    label: str = "",
    stream_to_parent: bool = True,
    parent_approval_callback=None,
    parent_workspace: str | None = None,
) -> dict:
    """
    Core sub-agent runner with isolated context.

    Sub-agents receive only the task and an optional explicit `context`
    string from the caller — never raw parent messages or tool results.
    Esse isolamento e proposital: messages do parent podem conter saida de
    URLs/arquivos controlados por atacante, e injeta-las no prompt do
    sub-agent vira vetor de prompt-injection cross-agent.
    """

    # Lazy imports to avoid circular dependencies
    from ..agent import run_agent
    from ..config import (
        DEFAULT_PROVIDER,
        FEATURES as feat,
        LIMITS,
        get_subagent_allow,
        get_subagent_extra_block,
        get_subagent_policy,
    )
    from . import get_openai_tools, get_tool

    max_iterations = LIMITS.get("subagent_max_iterations", feat.get("subagent_max_iterations", 15))
    agent_provider = provider or DEFAULT_PROVIDER
    workspace_root = parent_workspace or str(AGENT_WORKSPACE)

    agent_id = _new_agent_id()
    try:
        scratch_dir = _create_scratch_dir(workspace_root, agent_id)
    except OSError as e:
        return {"error": f"Cannot create scratch dir for sub-agent: {e}"}

    # Build isolated context for the sub-agent
    system_prompt = _load_subagent_prompt()

    scratch_rel = scratch_dir.relative_to(workspace_root)
    task_content = (
        f"[AGENT_ID: {agent_id}]\n"
        f"[SCRATCH_DIR: {scratch_rel}]  (relative to workspace)\n"
        "Write any artifacts, logs, or intermediate files to SCRATCH_DIR. "
        "You may read anything under the workspace using relative paths.\n\n"
    )
    if context:
        task_content += f"Context: {context}\n\n"
    task_content += task

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_content},
    ]

    # Get tools — filter out delegate tools to prevent recursion
    _blocked = {"delegate_task", "delegate_parallel"}
    all_tools = get_openai_tools()
    policy = get_subagent_policy()
    if parent_approval_callback is None and policy != "relaxed":
        _blocked = _blocked | SUBAGENT_DESTRUCTIVE_BLOCKLIST
    _blocked = _blocked | get_subagent_extra_block()
    _blocked = _blocked - get_subagent_allow()
    _blocked = _blocked | {"delegate_task", "delegate_parallel"}
    tools = [t for t in all_tools if t["function"]["name"] not in _blocked]

    if tools_filter:
        allowed = {s.strip() for s in tools_filter.split(",")}
        tools = [t for t in tools if t["function"]["name"] in allowed]
    else:
        allowed = None

    original_get_tool = get_tool
    _all_blocked = _blocked
    allowed_filter = allowed

    def _safe_get_tool(name: str):
        if name in _all_blocked:
            return None
        if allowed_filter is not None and name not in allowed_filter:
            return None
        return original_get_tool(name)

    is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    should_stream = stream_to_parent and is_tty

    collected_text = ""
    tool_calls_made = []
    errors = []

    effective_approval = parent_approval_callback or _auto_approve_no_callback

    try:
        async for event in run_agent(
            messages=messages,
            user_message=task,
            temperature=0.3,
            provider=agent_provider,
            get_tool_fn=_safe_get_tool,
            tools=tools,
            approval_callback=effective_approval,
            max_iterations=max_iterations,
            workspace=workspace_root,
        ):
            if event["type"] == "token":
                collected_text += event.get("text", "")
            elif event["type"] == "tool_call":
                tool_calls_made.append(event["name"])
                if should_stream:
                    print_subagent_event(event, label)
            elif event["type"] == "done":
                collected_text = event.get("reply", collected_text)
                if should_stream:
                    print_subagent_event(event, label)
            elif event["type"] == "error":
                errors.append(event.get("message", "unknown error"))
    except Exception as e:
        logger.error(f"Sub-agent {agent_id} failed: {e}", exc_info=True)
        return {
            "error": f"Sub-agent execution failed: {type(e).__name__}: {e}",
            "agent_id": agent_id,
        }
    finally:
        # #D025: cleanup do scratch dir mesmo em CancelledError / Ctrl+C.
        try:
            if scratch_dir.exists() and not any(scratch_dir.iterdir()):
                scratch_dir.rmdir()
        except OSError:
            pass

    scratch_files = await asyncio.to_thread(_snapshot_dir, scratch_dir)

    result = {
        "status": "completed",
        "result": collected_text,
        "tools_used": tool_calls_made,
        "iterations": len(tool_calls_made),
        "agent_id": agent_id,
        "scratch_dir": str(scratch_dir),
        "scratch_files": scratch_files,
    }
    if errors:
        result["errors"] = errors

    return result


# ── Single delegation ─────────────────────────────────────────

async def _delegate_task(
    task: str,
    context: str = "",
    tools_filter: str = "",
    provider: str = "",
) -> dict:
    """Spawn a single sub-agent to handle a task."""
    if not FEATURES.get("multi_agent_enabled"):
        return {"error": "Multi-agent system is disabled. Set FEATURES['multi_agent_enabled']=True."}
    if not FEATURES.get("delegate_tool_enabled"):
        return {"error": "Delegate tool is disabled. Enable 'delegate_tool_enabled' in config."}
    return await _run_subagent(task, context, tools_filter, provider)


# ── Parallel delegation ───────────────────────────────────────

async def _delegate_parallel(
    tasks: str,
    context: str = "",
    tools_filter: str = "",
    provider: str = "",
) -> dict:
    """Spawn multiple sub-agents in parallel, each handling one task."""
    if not FEATURES.get("multi_agent_enabled"):
        return {"error": "Multi-agent system is disabled. Set FEATURES['multi_agent_enabled']=True."}
    if not FEATURES.get("delegate_tool_enabled"):
        return {"error": "Delegate tool is disabled. Enable 'delegate_tool_enabled' in config."}

    try:
        task_list = json.loads(tasks)
        if not isinstance(task_list, list) or not task_list:
            return {"error": "tasks must be a non-empty JSON array of strings"}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON in tasks: {e}"}

    max_total = FEATURES.get("max_delegate_total_tasks", 10)
    if len(task_list) > max_total:
        return {
            "error": (
                f"Too many tasks ({len(task_list)}). Maximum is {max_total}. "
                "Split into smaller batches or reconsider scope."
            ),
            "submitted": len(task_list),
            "limit": max_total,
        }

    max_parallel = FEATURES.get("max_parallel_agents", 3)
    semaphore = asyncio.Semaphore(max_parallel)

    async def _run_with_limit(idx: int, task_desc: str) -> dict:
        async with semaphore:
            logger.info(f"Sub-agent #{idx + 1} starting: {task_desc[:60]}")
            result = await _run_subagent(
                task_desc, context, tools_filter, provider,
                label=f"#{idx + 1}",
            )
            result["task_index"] = idx
            result["task"] = task_desc
            return result

    coros = [_run_with_limit(i, t) for i, t in enumerate(task_list)]
    results = await asyncio.gather(*coros, return_exceptions=True)

    formatted = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            formatted.append({
                "task_index": i,
                "task": task_list[i],
                "status": "failed",
                "error": str(r),
            })
        else:
            formatted.append(r)

    succeeded = sum(1 for r in formatted if r.get("status") == "completed")
    failed = len(formatted) - succeeded

    return {
        "total_tasks": len(task_list),
        "succeeded": succeeded,
        "failed": failed,
        "results": formatted,
    }


# ── Registration ──────────────────────────────────────────────

register_tool(
    ToolDefinition(
        name="delegate_task",
        description=(
            "Delegate a task to a sub-agent that runs independently with its own tool loop. "
            "Use for focused investigation tasks that don't need the main conversation context. "
            "The sub-agent has access to read-only and safe tools only (destructive tools are blocked). "
            "Requires user approval. For multiple independent tasks, use delegate_parallel instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Clear description of what the sub-agent should do. "
                        "Be specific — the sub-agent has no context from the current conversation."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "Optional context (file paths, constraints, what you've tried).",
                    "default": "",
                },
                "tools_filter": {
                    "type": "string",
                    "description": (
                        "Optional comma-separated tool names the sub-agent can use. "
                        "Empty = all tools. Example: 'read_file,search_files,glob_files'"
                    ),
                    "default": "",
                },
                "provider": {
                    "type": "string",
                    "description": "LLM provider override. Defaults to main agent's provider.",
                    "default": "",
                },
            },
            "required": ["task"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        executor=_delegate_task,
        category=ToolCategory.AGENT,
    )
)

register_tool(
    ToolDefinition(
        name="delegate_parallel",
        description=(
            "Run multiple sub-agents in PARALLEL, each handling one independent task. "
            "Much faster than sequential delegate_task calls for independent work. "
            "Concurrency is limited to max_parallel_agents (default: 3). "
            "Example: analyze 3 different modules simultaneously."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "string",
                    "description": (
                        'JSON array of task descriptions. Each task runs in its own sub-agent. '
                        'Example: \'["analyze alpha/agent.py", "analyze alpha/llm.py", "analyze alpha/executor.py"]\''
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "Shared context passed to ALL sub-agents.",
                    "default": "",
                },
                "tools_filter": {
                    "type": "string",
                    "description": "Comma-separated tool names available to ALL sub-agents.",
                    "default": "",
                },
                "provider": {
                    "type": "string",
                    "description": "LLM provider override for all sub-agents.",
                    "default": "",
                },
            },
            "required": ["tasks"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        executor=_delegate_parallel,
        category=ToolCategory.AGENT,
    )
)
