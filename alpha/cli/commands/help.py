"""/help — list available slash commands."""

from __future__ import annotations

from alpha.display import C, c

from ._types import DispatchResult, ReplContext


def _handle_help(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    print(f"  {c(C.CYAN, '/init')}     — Draft an ALPHA.md for this project")
    print(f"  {c(C.CYAN, '/clear')}    — Clear history and screen")
    print(f"  {c(C.CYAN, '/history')}  — Show conversation history")
    print(f"  {c(C.CYAN, '/save')}     — Save current session")
    print(f"  {c(C.CYAN, '/load')}     — Load a previous session")
    print(f"  {c(C.CYAN, '/continue')} — Resume from last session")
    print(f"  {c(C.CYAN, '/sessions')} — List saved sessions")
    print(f"  {c(C.CYAN, '/context')}  — Show context window usage")
    print(f"  {c(C.CYAN, '/cost')}     — Show token usage and estimated USD cost for this session")
    print(f"  {c(C.CYAN, '/stats')}    — Show iteration/tool/approval stats for this session")
    print(f"  {c(C.CYAN, '/memory')}   — Manage cross-session memory (list|forget <i>|clear|edit [workspace|global])")
    print(f"  {c(C.CYAN, '/accept-edits')} — Toggle auto-approve for destructive tools (shift+tab)")
    print(f"  {c(C.CYAN, '/tools')}    — List available tools")
    print(f"  {c(C.CYAN, '/skills')}   — List registered skills (ready vs inactive)")
    print(f"  {c(C.CYAN, '/mcp')}      — List connected MCP servers")
    print(f"  {c(C.CYAN, '/image')}    — Attach an image (Ctrl+V or Alt+V also works)")
    print(f"  {c(C.CYAN, '/pdf')}      — Attach a PDF (text extracted via pypdf)")
    print(f"  {c(C.CYAN, '/audio')}    — Attach audio (transcribed via OpenAI Whisper)")
    print(f"  {c(C.CYAN, '/agents')}   — List named agents")
    print(f"  {c(C.CYAN, '/agent')}    — Show/switch active agent")
    print(f"  {c(C.CYAN, '/model')}    — Show/switch provider & model")
    print(f"  {c(C.CYAN, '/sandbox')}  — Show sandbox state for destructive shell tools")
    print(f"  {c(C.CYAN, '/<skill>')}  — Invoke a skill by name (e.g. /skill-creator)")
    print(f"  {c(C.CYAN, '/exit')}     — Exit")
    return DispatchResult.CONTINUE
