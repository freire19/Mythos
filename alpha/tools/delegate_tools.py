"""Delegate tools — spawn sub-agents to handle tasks independently.

Supports single delegation (delegate_task) and parallel delegation
(delegate_parallel) with concurrency limited by max_parallel_agents.

Apos #082 split: helpers extraidos para _delegate_core.py.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ..config import FEATURES
from ..display import print_subagent_event
from ..display.core import flush_subagent_dup, _tool_args_preview
from .workspace import AGENT_WORKSPACE

from ._delegate_core import (
    SUBAGENT_DESTRUCTIVE_BLOCKLIST,
    GIT_READ_ACTIONS,
    _auto_approve_no_callback,
    _load_subagent_prompt,
    _new_agent_id,
    _create_scratch_dir,
    _snapshot_dir,
    _strip_control_chars,
)

logger = logging.getLogger(__name__)



def _build_subagent_messages(task: str, context: str, agent_id: str, scratch_rel) -> list[dict]:
    """System + user messages with the SCRATCH_DIR preamble and stripped
    control chars. Paths relativos no contexto do sub-agent (#022): o
    workspace absoluto nao precisa estar no prompt — vazar o absoluto
    deixava ele acessivel via tool results e logs."""
    task_content = (
        f"[AGENT_ID: {agent_id}]\n"
        f"[SCRATCH_DIR: {scratch_rel}]  (relative to workspace)\n"
        "Write any artifacts, logs, or intermediate files to SCRATCH_DIR. "
        "You may read anything under the workspace using relative paths.\n\n"
    )
    if context:
        task_content += f"Context: {_strip_control_chars(context)}\n\n"
    task_content += _strip_control_chars(task)
    return [
        {"role": "system", "content": _load_subagent_prompt()},
        {"role": "user", "content": task_content},
    ]


def _resolve_subagent_blocklist(parent_approval_callback) -> set[str]:
    """Compose the runtime blocklist from policy + extra_block + allow.

    `delegate_*` is in the blocklist twice on purpose — once from the
    fixed anti-recursion set (so it can't slip into `subagent_allow`),
    and once after subtracting `subagent_allow` so even an explicit user
    override can't enable cross-agent recursion.

    #D007: policy/extra_block/allow vem de env via getters
    (AUDIT_V1.2 #014: cache de import-time perdia mudancas runtime).
    """
    from ..config import (
        get_subagent_allow,
        get_subagent_extra_block,
        get_subagent_policy,
    )
    blocked: set[str] = {"delegate_task", "delegate_parallel", "delegate_consensus"}
    policy = get_subagent_policy()
    if parent_approval_callback is None and policy != "relaxed":
        blocked = blocked | SUBAGENT_DESTRUCTIVE_BLOCKLIST
    blocked = (blocked | get_subagent_extra_block()) - get_subagent_allow()
    blocked = blocked | {"delegate_task", "delegate_parallel", "delegate_consensus"}
    return blocked


def _build_subagent_tools(tools_filter: str, blocked: set[str]):
    """Pick the OpenAI-format tool list for this sub-agent.

    Returns (tools list, allowed-name set or None). When `tools_filter`
    is empty, `allowed` is None — `_make_safe_get_tool` interprets that
    as "no name-set restriction beyond the blocklist".
    """
    from . import get_openai_tools
    all_tools = get_openai_tools()
    tools = [t for t in all_tools if t["function"]["name"] not in blocked]

    allowed: set[str] | None = None
    if tools_filter:
        allowed = {s.strip() for s in tools_filter.split(",")}
        tools = [t for t in tools if t["function"]["name"] in allowed]
    return tools, allowed


def _make_safe_get_tool(blocked: set[str], allowed: set[str] | None, original_get_tool):
    """Closure that gates tool lookup by blocklist + optional allow-filter.

    Centralizes the gate (#091) so a sub-agent can't reach into
    TOOL_REGISTRY directly to bypass blocked/filtered tools.
    """
    def _safe_get_tool(name: str):
        if name in blocked:
            return None
        if allowed is not None and name not in allowed:
            return None
        return original_get_tool(name)
    return _safe_get_tool


async def _run_subagent(
    task: str,
    context: str = "",
    tools_filter: str = "",
    provider: str = "",
    label: str = "",
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
    from ..config import DEFAULT_PROVIDER
    from . import get_tool

    max_iterations = FEATURES.get("subagent_max_iterations", 15)
    agent_provider = provider or DEFAULT_PROVIDER
    workspace_root = parent_workspace or str(AGENT_WORKSPACE)

    agent_id = _new_agent_id()
    try:
        scratch_dir = _create_scratch_dir(workspace_root, agent_id)
    except OSError as e:
        return {"ok": False, "category": "io_error", "error": f"Cannot create scratch dir for sub-agent: {e}"}

    messages = _build_subagent_messages(
        task, context, agent_id, scratch_dir.relative_to(workspace_root)
    )
    blocked = _resolve_subagent_blocklist(parent_approval_callback)
    tools, allowed = _build_subagent_tools(tools_filter, blocked)
    safe_get_tool = _make_safe_get_tool(blocked, allowed, get_tool)

    # Run sub-agent loop
    collected_text = ""
    tool_calls_made = []
    errors = []

    # Sub-agents use parent's approval callback if available, otherwise the
    # module-level _auto_approve_no_callback gate (handles git_operation
    # read/write distinction).
    effective_approval = parent_approval_callback or _auto_approve_no_callback

    try:
        async for event in run_agent(
            messages=messages,
            user_message=task,
            temperature=0.3,
            provider=agent_provider,
            get_tool_fn=safe_get_tool,
            tools=tools,
            approval_callback=effective_approval,
            max_iterations=max_iterations,
            workspace=workspace_root,
        ):
            if event["type"] == "token":
                collected_text += event.get("text", "")
            elif event["type"] == "tool_call":
                tool_calls_made.append({
                    "name": event["name"],
                    "args_preview": _tool_args_preview(event.get("args", {})),
                })
                # Surface sub-agent tool calls live so the REPL doesn't look frozen.
                print_subagent_event(event, label)
            elif event["type"] == "done":
                collected_text = event.get("reply", collected_text)
            elif event["type"] == "error":
                errors.append(event.get("message", "unknown error"))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # #056: log full traceback (logger.error sem exc_info perdia o
        # frame onde o bug realmente aconteceu).
        logger.error(f"Sub-agent {agent_id} failed: {e}", exc_info=True)
        return {
            "ok": False,
            "category": "subagent_error",
            "error": f"Sub-agent execution failed: {type(e).__name__}: {e}",
            "agent_id": agent_id,
        }
    finally:
        # Emit trailing `(×N)` so the last dedup count isn't lost.
        flush_subagent_dup(label or "_")
        # Cleanup on every exit path; keep scratch dir if sub-agent wrote artifacts.
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
        return {"ok": False, "category": "feature_disabled", "error": "Multi-agent system is disabled. Set FEATURES['multi_agent_enabled']=True."}
    if not FEATURES.get("delegate_tool_enabled"):
        return {"ok": False, "category": "feature_disabled", "error": "Delegate tool is disabled. Enable 'delegate_tool_enabled' in config."}
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
        return {"ok": False, "category": "feature_disabled", "error": "Multi-agent system is disabled. Set FEATURES['multi_agent_enabled']=True."}
    if not FEATURES.get("delegate_tool_enabled"):
        return {"ok": False, "category": "feature_disabled", "error": "Delegate tool is disabled. Enable 'delegate_tool_enabled' in config."}

    # Parse tasks JSON array
    try:
        task_list = json.loads(tasks)
        if not isinstance(task_list, list) or not task_list:
            return {"ok": False, "category": "invalid_args", "error": "tasks must be a non-empty JSON array of strings"}
    except json.JSONDecodeError as e:
        return {"ok": False, "category": "invalid_args", "error": f"Invalid JSON in tasks: {e}"}

    # Cap total: max_parallel_agents so controla concorrencia, nao total.
    # Sem cap, modelo pode submeter array de 100 tarefas — runaway de custo
    # e disco (cada sub-agent gasta 15 iteracoes + scratch dir).
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

    # Launch all sub-agents concurrently (limited by semaphore)
    coros = [_run_with_limit(i, t) for i, t in enumerate(task_list)]
    results = await asyncio.gather(*coros, return_exceptions=True)

    # Format results
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


# ── Consensus delegation (Plano-Upgrade-v3 H2 #8) ─────────────
#
# N sub-agents answer the SAME question; we aggregate by text similarity
# and report majority/dissent. The aggregation is mechanical — the
# caller still decides what to do with the result (a contested code-review
# verdict is itself useful information).
#
# Why a separate tool instead of a `consensus=True` flag on
# delegate_parallel: the two have different shapes. delegate_parallel
# distributes N tasks; delegate_consensus distributes 1 task N times.
# Merging the signatures would force every caller to learn both modes.

_CONSENSUS_SIMILARITY_THRESHOLD = 0.7
_CONSENSUS_ANSWER_PREVIEW_CHARS = 4000


def _cluster_answers(answers: list[str]) -> list[list[int]]:
    """Greedy single-link clustering by SequenceMatcher ratio.

    Each cluster is a list of indices into `answers`. Empty / whitespace-only
    answers share a single degenerate cluster so N failed sub-agents don't
    surface as N dissent groups."""
    empty_indices: list[int] = []
    non_empty: list[tuple[int, str]] = []
    for idx, ans in enumerate(answers):
        normalized = (ans or "").strip()[:_CONSENSUS_ANSWER_PREVIEW_CHARS]
        if not normalized:
            empty_indices.append(idx)
        else:
            non_empty.append((idx, normalized))

    clusters: list[list[int]] = []
    representatives: list[str] = []
    if empty_indices:
        clusters.append(empty_indices)
        representatives.append("")

    for idx, normalized in non_empty:
        placed = False
        for ci, rep in enumerate(representatives):
            if not rep:
                continue
            ratio = difflib.SequenceMatcher(None, normalized, rep).ratio()
            if ratio >= _CONSENSUS_SIMILARITY_THRESHOLD:
                clusters[ci].append(idx)
                placed = True
                break
        if not placed:
            clusters.append([idx])
            representatives.append(normalized)
    return clusters


async def _delegate_consensus(
    question: str,
    n: int = 3,
    context: str = "",
    tools_filter: str = "",
    provider: str = "",
) -> dict:
    """N sub-agents answer the SAME question; return majority + dissent.

    Use for contested questions where one agent might miss something but
    most won't: "is this a bug?", "does this PR meet the spec?", code
    review, security audit reads."""
    if not FEATURES.get("multi_agent_enabled"):
        return {"ok": False, "category": "feature_disabled", "error": "Multi-agent system is disabled. Set FEATURES['multi_agent_enabled']=True."}
    if not FEATURES.get("delegate_tool_enabled"):
        return {"ok": False, "category": "feature_disabled", "error": "Delegate tool is disabled. Enable 'delegate_tool_enabled' in config."}

    max_parallel = FEATURES.get("max_parallel_agents", 3)
    if not isinstance(n, int) or n < 2:
        return {"ok": False, "category": "invalid_args", "error": "n must be an integer >= 2 (consensus is meaningless below 2)"}
    if n > max_parallel:
        return {
            "ok": False, "category": "invalid_args",
            "error": f"n={n} exceeds max_parallel_agents={max_parallel}. Lower n or raise the cap.",
        }

    semaphore = asyncio.Semaphore(max_parallel)

    async def _run_with_limit(idx: int) -> dict:
        async with semaphore:
            logger.info(f"Consensus agent #{idx + 1}/{n} starting")
            result = await _run_subagent(
                question, context, tools_filter, provider,
                label=f"#{idx + 1}",
            )
            result["agent_index"] = idx
            return result

    raw_results = await asyncio.gather(
        *(_run_with_limit(i) for i in range(n)),
        return_exceptions=True,
    )

    # Normalize shapes (Exception -> dict with status=failed)
    normalized: list[dict] = []
    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            normalized.append({
                "agent_index": i,
                "status": "failed",
                "result": "",
                "error": str(r),
            })
        else:
            normalized.append(r)

    # Cluster only successful agents — failures get reported separately
    # and never influence majority detection.
    successful = [r for r in normalized if r.get("status") == "completed"]
    answer_texts = [r.get("result", "") for r in successful]

    if not successful:
        return {
            "ok": False,
            "category": "all_failed",
            "question": question,
            "n_agents": n,
            "failed": len(normalized),
            "errors": [r.get("error", "unknown") for r in normalized],
        }

    clusters = _cluster_answers(answer_texts)
    # Sort clusters by size (desc) then by first agent index for stable order
    clusters.sort(key=lambda c: (-len(c), c[0]))
    majority_cluster = clusters[0]
    majority_size = len(majority_cluster)

    # Map cluster-local indices back to original agent indices via `successful`
    def _agents_in(cluster: list[int]) -> list[int]:
        return [successful[i]["agent_index"] + 1 for i in cluster]

    majority_agents = _agents_in(majority_cluster)
    majority_answer = successful[majority_cluster[0]].get("result", "")

    dissent = []
    for c in clusters[1:]:
        rep_idx = c[0]
        dissent.append({
            "agents": _agents_in(c),
            "answer": successful[rep_idx].get("result", ""),
        })

    consensus_reached = majority_size > n // 2  # strict majority

    return {
        "ok": True,
        "question": question,
        "n_agents": n,
        "n_successful": len(successful),
        "n_failed": n - len(successful),
        "answers": [
            {
                "agent": r["agent_index"] + 1,
                "status": r.get("status", "unknown"),
                "answer": r.get("result", ""),
                "error": r.get("error"),
            }
            for r in normalized
        ],
        "consensus": {
            "reached": consensus_reached,
            "majority": {
                "size": majority_size,
                "agents": majority_agents,
                "answer": majority_answer,
            },
            "dissent": dissent,
        },
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

register_tool(
    ToolDefinition(
        name="delegate_consensus",
        description=(
            "Run N sub-agents on the SAME question in parallel and return "
            "majority + dissent. Use for contested verdicts where a single "
            "agent might miss something: 'is this a bug?', code review, "
            "security audit reads, scope checks. Aggregates answers by "
            "text similarity (>=0.7 same cluster). Output includes per-agent "
            "answers plus a majority cluster + dissenting clusters with the "
            "specific agents on each side. Requires user approval."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The single question/task all sub-agents answer. "
                        "Make it specific and answerable in one short reply — "
                        "ambiguous questions produce scattered clusters."
                    ),
                },
                "n": {
                    "type": "integer",
                    "description": (
                        "Number of sub-agents to run (default 3). Must be "
                        ">=2 and <=max_parallel_agents."
                    ),
                    "default": 3,
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
            "required": ["question"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        executor=_delegate_consensus,
        category=ToolCategory.AGENT,
    )
)
