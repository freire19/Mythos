"""Mythos terminal display.

Re-exports from submodules for backward compatibility:
- core: colors (C, c), formatting helpers, tool output, approval UI
- thinking: ThinkingIndicator spinner + module-level singletons
"""

from __future__ import annotations

from .core import (
    C,
    DISPLAY_LINE_TRUNCATE,
    DISPLAY_MAX_LINES,
    DISPLAY_PREVIEW_TRUNCATE,
    DISPLAY_PROMPT_VALUE_TRUNCATE,
    _approve_all,  # noqa: F401 — back-compat for monkeypatch-based tests
    _display_tool_name,
    _format_duration,
    _format_tokens,
    _hint_for,
    _print_plan_card,
    _print_todo_list,
    _render_diff,
    _TODO_STATUS_GLYPH,
    c,
    format_context_indicator,
    is_auto_accept,
    label_for_tool,
    print_approval_request,
    print_banner,
    print_context_compressed,
    print_context_warning,
    print_error,
    print_iteration_status,
    print_phase,
    print_providers_list,
    print_sessions_list,
    print_silent_turn,
    print_subagent_event,
    print_tool_call,
    print_tool_result,
    print_tools_list,
    render_markdown,
    reset_approve_all,
    set_auto_accept,
    supports_color,
    toggle_auto_accept,
)

from .thinking import (
    ThinkingIndicator,
    cleanup_indicator,
    get_active_indicator,
    get_pinned_todos,
    set_pinned_todos,
)

__all__ = [
    "C",
    "DISPLAY_LINE_TRUNCATE",
    "DISPLAY_MAX_LINES",
    "DISPLAY_PREVIEW_TRUNCATE",
    "DISPLAY_PROMPT_VALUE_TRUNCATE",
    "ThinkingIndicator",
    "_TODO_STATUS_GLYPH",
    "c",
    "cleanup_indicator",
    "format_context_indicator",
    "get_active_indicator",
    "get_pinned_todos",
    "is_auto_accept",
    "label_for_tool",
    "print_approval_request",
    "print_banner",
    "print_context_compressed",
    "print_context_warning",
    "print_error",
    "print_iteration_status",
    "print_phase",
    "print_providers_list",
    "print_sessions_list",
    "print_subagent_event",
    "print_tool_call",
    "print_tool_result",
    "print_tools_list",
    "render_markdown",
    "reset_approve_all",
    "set_auto_accept",
    "set_pinned_todos",
    "supports_color",
    "toggle_auto_accept",
]
