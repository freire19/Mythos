"""Workspace enforcement — validates tool args against an agent's workspace.

If an AgentScope has a workspace set, any tool that touches the filesystem
must have its path arguments resolved within that workspace. Absolute paths
outside the workspace are rejected; relative paths are rewritten to be
workspace-relative.
"""

from __future__ import annotations

import contextvars
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# #015: the workspace active for the current tool dispatch. The executor sets
# this around `execute_tool_calls` so composite tools' inner `_run_tool` calls
# (which bypass the executor's approval/workspace gate per the #D110 trust
# model) can re-enforce workspace bounds on sub-tool args they build
# internally. None = no workspace restriction (main agent).
active_workspace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "alpha_active_workspace", default=None
)

# Map of tool_name -> list of arg names that carry filesystem paths.
# Tools not in this map receive no workspace validation (they either don't
# touch the filesystem or have their own sandboxing).
PATH_PARAMS_BY_TOOL: dict[str, list[str]] = {
    "read_file": ["path"],
    "write_file": ["path"],
    "edit_file": ["path"],
    "list_directory": ["path"],
    "search_files": ["path"],
    "glob_files": ["path"],
    "search_and_replace": ["path"],
    "execute_shell": ["cwd"],
    "execute_pipeline": ["cwd"],
    "git_operation": ["path"],
    "run_tests": ["path"],
    "project_overview": ["path"],
}

# #DL-014: list-typed path params — EACH element is validated against the
# workspace. The old code only iterated scalar string params, so a list arg
# (e.g. `git_operation(action="blame", files=["/etc/passwd"])`) bypassed the
# `relative_to(ws)` gate entirely (str(list) was never a real path). These are
# never defaulted to the workspace — an empty/absent list stays as-is.
PATH_LIST_PARAMS_BY_TOOL: dict[str, list[str]] = {
    "git_operation": ["files"],
}

# Tools where an empty path should default to the workspace instead of CWD.
DEFAULT_TO_WORKSPACE: set[str] = {
    "execute_shell",
    "execute_pipeline",
    "git_operation",
    "run_tests",
    "project_overview",
}


@lru_cache(maxsize=64)
def _resolved_ws(workspace: str) -> Path:
    return Path(workspace).expanduser().resolve()


def _resolve_in_workspace(
    val, ws: Path, tool_name: str, param: str
) -> tuple[bool, str, str]:
    """Resolve a single path value against the workspace.

    Returns (ok, resolved_str, error_msg). Shared by scalar and list path
    params so the traversal-log + resolve + relative_to gate stays in one
    place (#DL-014)."""
    raw = str(val)
    p = Path(raw).expanduser()

    # AUDIT_V1.10 NP-002: detectar tentativas de path traversal (..) antes da
    # normalizacao. O check `relative_to(ws)` abaixo ja protege a borda de
    # seguranca; logamos a *intencao* de escapar sem rejeitar — o resolve() +
    # relative_to() continua sendo o gate real.
    if ".." in p.parts:
        logger.warning(
            "Path traversal attempt detected: tool=%s param=%s raw=%r resolved=%s",
            tool_name, param, raw, str(ws / p if not p.is_absolute() else p),
        )

    if not p.is_absolute():
        p = ws / p

    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as e:
        return False, "", f"Cannot resolve path '{val}': {e}"

    try:
        resolved.relative_to(ws)
    except ValueError:
        return False, "", (
            f"Path '{val}' is outside the agent's workspace.\n"
            f"  Workspace: {ws}\n"
            f"  Resolved:  {resolved}\n"
            f"Access denied."
        )

    return True, str(resolved), ""


def validate_args(
    workspace: str,
    tool_name: str,
    args: dict,
) -> tuple[bool, dict, str]:
    """Validate/rewrite tool args against the workspace.

    Returns:
        (ok, new_args, error_msg)
        - ok=True: args validated (possibly rewritten with absolute workspace-relative paths)
        - ok=False: args rejected; error_msg explains why
    """
    params = PATH_PARAMS_BY_TOOL.get(tool_name)
    list_params = PATH_LIST_PARAMS_BY_TOOL.get(tool_name)
    if not params and not list_params:
        return True, args, ""

    ws = _resolved_ws(str(workspace))
    new_args = dict(args)

    # Scalar path params (path/cwd) — empty may default to the workspace.
    for param in (params or []):
        val = args.get(param)
        if not val:
            if tool_name in DEFAULT_TO_WORKSPACE:
                new_args[param] = str(ws)
            continue
        ok, resolved, err = _resolve_in_workspace(val, ws, tool_name, param)
        if not ok:
            return False, args, err
        new_args[param] = resolved

    # #DL-014: list-typed path params (e.g. git_operation.files) — validate
    # EACH element. Never default an empty list to the workspace.
    for param in (list_params or []):
        val = args.get(param)
        if not val:
            continue
        items = val if isinstance(val, list) else [val]
        new_items = []
        for item in items:
            if not item:
                new_items.append(item)
                continue
            ok, resolved, err = _resolve_in_workspace(item, ws, tool_name, param)
            if not ok:
                return False, args, err
            new_items.append(resolved)
        new_args[param] = new_items

    return True, new_args, ""
