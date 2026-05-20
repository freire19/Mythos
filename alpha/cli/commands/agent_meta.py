"""Agent/model meta handlers: /agent, /agents, /model, /tools, /skills, /mcp, /init."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from alpha.agents import get_agent
from alpha.config import get_available_providers, get_provider_config
from alpha.display import (
    C,
    c,
    print_error,
    print_tools_list,
)
from alpha.history import generate_session_id
from alpha.mcp import list_active_servers as list_mcp_servers
from alpha.skills import list_skills

from ..setup import build_system_prompt, get_tools_for_agent, list_agents
from ._types import DispatchResult, ReplContext


def _handle_tools(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    print_tools_list(ctx.tools)
    return DispatchResult.CONTINUE


def _handle_skills(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    skills = sorted(list_skills(), key=lambda s: s.name)
    if not skills:
        print(c(C.GRAY, "  No skills registered."))
        return DispatchResult.CONTINUE

    ready: list = []
    inactive: list = []
    for s in skills:
        missing = [b for b in s.requires_bins if not shutil.which(b)]
        (inactive if missing else ready).append((s, missing))

    summary = (
        f"{len(skills)} skills registered "
        f"({len(ready)} ready, {len(inactive)} inactive)"
    )
    print(f"  {c(C.GRAY, summary)}")
    print(f"  {c(C.GRAY, 'Invoke with /<skill-name> [args]')}")
    print()
    if ready:
        print(f"  {c(C.GREEN + C.BOLD, 'Ready')}")
        for s, _ in ready:
            desc = (s.description or "").strip().split("\n", 1)[0]
            print(
                f"  {c(C.GREEN, '✦')} {c(C.CYAN, s.name):<24} "
                f"{c(C.GRAY, desc[:90])}"
            )
        print()
    if inactive:
        print(f"  {c(C.YELLOW + C.BOLD, 'Inactive (missing bins)')}")
        for s, missing in inactive:
            print(
                f"  {c(C.YELLOW, '○')} {c(C.GRAY, s.name):<24} "
                f"{c(C.GRAY, 'needs: ' + ', '.join(missing))}"
            )
    return DispatchResult.CONTINUE


def _handle_mcp(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    servers = list_mcp_servers()
    if not servers:
        print(c(C.GRAY, "  No MCP servers connected. Configure .alpha/mcp.json"))
    else:
        for s in servers:
            tool_names = ", ".join(s["tools"]) or c(C.GRAY, "(no tools)")
            print(f"  {c(C.CYAN, s['name']):30s} {tool_names}")
    return DispatchResult.CONTINUE


def _handle_agents(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    agents = list_agents()
    if not agents:
        print(c(C.GRAY, "  No agents defined. Create ./agents/<name>/agent.yaml"))
    else:
        current = ctx.active_agent.name if ctx.active_agent else None
        for a in agents:
            marker = c(C.GREEN, "●") if a.name == current else " "
            desc = a.description or c(C.GRAY, "(no description)")
            print(f"  {marker} {c(C.CYAN, a.name):30s} {desc}")
    return DispatchResult.CONTINUE


def _handle_agent(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    if len(parts) < 2:
        name = ctx.active_agent.name if ctx.active_agent else "(none)"
        print(f"  {c(C.GRAY, 'Active agent:')} {name}")
        print(f"  {c(C.GRAY, 'Usage: /agent <name>  (or /agent none to clear)')}")
        return DispatchResult.CONTINUE

    target = parts[1]
    if target in ("none", "clear", "off"):
        ctx.active_agent = None
    else:
        picked = get_agent(target)
        if picked is None:
            print(c(C.RED, f"  Agent not found: {target}"))
            return DispatchResult.CONTINUE
        ctx.active_agent = picked

    # Re-apply scope
    if ctx.active_agent and ctx.active_agent.provider:
        ctx.provider = ctx.active_agent.provider
    if ctx.active_agent and ctx.active_agent.temperature is not None:
        ctx.temperature = ctx.active_agent.temperature
    ctx.cfg = get_provider_config(ctx.provider)
    if ctx.active_agent and ctx.active_agent.model:
        ctx.cfg["model"] = ctx.active_agent.model
    ctx.system_prompt = build_system_prompt(ctx.active_agent)
    ctx.get_tool_fn, ctx.tools = get_tools_for_agent(ctx.active_agent)
    ctx.messages[:] = [{"role": "system", "content": ctx.system_prompt}]
    ctx.history.clear()
    ctx.session_id = generate_session_id()
    name = ctx.active_agent.name if ctx.active_agent else "(none)"
    print(
        f"  {c(C.GREEN, '✓')} Switched to agent: {name} "
        f"({len(ctx.tools)} tools, provider={ctx.provider}, model={ctx.cfg['model']})"
    )
    return DispatchResult.CONTINUE


def _handle_model(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    providers_list = get_available_providers()

    target = None
    if len(parts) >= 2:
        target = parts[1]
    else:
        print(f"  {c(C.GRAY, 'Current:')} {c(C.CYAN, ctx.provider)} → {ctx.cfg['model']}")
        print(f"  {c(C.GRAY, 'Available:')}")
        for p in providers_list:
            mark = "●" if p["id"] == ctx.provider else " "
            color = C.GREEN if p["available"] else C.GRAY
            avail = "" if p["available"] else c(C.GRAY, " (no key)")
            print(f"    {c(color, mark)} {c(C.CYAN, p['id']):15} → {p['model']}{avail}")
        print(f"  {c(C.GRAY, 'Usage: /model <provider>')}")
        return DispatchResult.CONTINUE

    pick = next((p for p in providers_list if p["id"] == target), None)
    if pick is None:
        print(c(C.RED, f"  Unknown provider: {target}"))
        return DispatchResult.CONTINUE
    if not pick["available"]:
        print(c(C.RED, f"  {target} not available — set the API key first."))
        return DispatchResult.CONTINUE

    try:
        new_cfg = get_provider_config(target)
    except RuntimeError as e:
        print(c(C.RED, f"  Error: {e}"))
        return DispatchResult.CONTINUE

    ctx.provider = target
    ctx.cfg = new_cfg
    # Apply active_agent model override (e.g. named agent profiles)
    if ctx.active_agent and ctx.active_agent.model:
        ctx.cfg["model"] = ctx.active_agent.model
    # Rebuild system prompt for new provider and reset conversation state.
    # Carrying over messages from a different provider confuses smaller models.
    ctx.system_prompt = build_system_prompt(ctx.active_agent)
    ctx.messages[:] = [{"role": "system", "content": ctx.system_prompt}]
    ctx.history.clear()
    ctx.session_id = generate_session_id()
    print(
        f"  {c(C.GREEN, '✓')} Switched to {c(C.CYAN, ctx.provider)} → {ctx.cfg['model']}"
    )
    if not ctx.cfg["supports_tools"]:
        print(
            f"  {c(C.YELLOW, '⚠')} {c(C.GRAY, 'chat-only mode — tools disabled for this model')}"
        )
    return DispatchResult.CONTINUE


def _handle_init(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Synthesize a prompt asking the agent to draft an ALPHA.md and FALL THROUGH."""
    from alpha.project_context import CONTEXT_FILENAME

    target = Path(os.getcwd()) / CONTEXT_FILENAME
    force = "--force" in parts[1:]
    if target.exists() and not force:
        print_error(
            f"{CONTEXT_FILENAME} already exists at {target}. "
            f"Pass /init --force to overwrite, or delete it first."
        )
        return DispatchResult.CONTINUE

    action = "Overwrite the existing" if target.exists() else "Create a new"
    ctx.user_input_override = (
        f"[/init invoked]\n"
        f"{action} `{CONTEXT_FILENAME}` at {target} that captures "
        f"this project's stable context for Alpha. Steps:\n"
        f"1. Run project_overview to learn the project layout, type, and git status.\n"
        f"2. Read the key manifest(s): pyproject.toml / package.json / "
        f"Cargo.toml / pom.xml / Gemfile / go.mod / requirements.txt — whichever exist.\n"
        f"3. Read README.md if present, plus 1–2 source entry points (main.py, "
        f"src/index.ts, app/main.py, etc.) to confirm the actual stack.\n"
        f"4. Write {CONTEXT_FILENAME} with these sections, in this order:\n"
        f"   - `# {CONTEXT_FILENAME} — <project name>` (one-line title)\n"
        f"   - `## What this project is` — one short paragraph.\n"
        f"   - `## Stack & dependencies` — language version, key libs, package manager.\n"
        f"   - `## How to run / build / test` — exact commands the user types.\n"
        f"   - `## House rules` — conventions you can infer (test framework, "
        f"linter, type-hint policy, comment policy). Mark inferences explicitly.\n"
        f"   - `## Status & docs` — point to STATUS.md / docs/ if they exist.\n"
        f"   - `## Out-of-scope` — anything obviously off-limits "
        f"(e.g. don't edit prompts/, never commit secrets).\n"
        f"5. Keep the file under 4 KB. No filler. No emoji. "
        f"Use plain Markdown. Do not invent commands you have not verified.\n"
        f"6. After writing, print a one-line confirmation summarizing what you "
        f"included and remind the user to review before committing."
    )
    print(
        f"  {c(C.GREEN, '✦')} {c(C.CYAN, '/init')} "
        f"{c(C.GRAY, f'— drafting {CONTEXT_FILENAME} for {os.path.basename(os.getcwd())}')}"
    )
    return DispatchResult.FALL_THROUGH
