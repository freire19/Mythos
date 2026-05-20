"""Safety toggles: /accept-edits, /sandbox."""

from __future__ import annotations

from alpha.display import C, c, is_auto_accept, set_auto_accept

from ._types import DispatchResult, ReplContext


def _handle_accept_edits(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Toggle auto-approve mode for destructive tools (or set explicitly).

    Usage:
        /accept-edits          # toggle
        /accept-edits on       # force on
        /accept-edits off      # force off
    """
    if len(parts) > 1:
        arg = parts[1].lower()
        if arg in ("on", "true", "1", "yes", "y"):
            set_auto_accept(True)
        elif arg in ("off", "false", "0", "no", "n"):
            set_auto_accept(False)
        else:
            print(f"  {c(C.YELLOW, 'Usage:')} /accept-edits [on|off]")
            return DispatchResult.CONTINUE
    else:
        set_auto_accept(not is_auto_accept())

    if is_auto_accept():
        print(
            f"  {c(C.GREEN + C.BOLD, '»»')} "
            f"{c(C.GREEN, 'accept edits ON')} "
            f"{c(C.GRAY, '— destructive tools auto-approved this session')}"
        )
    else:
        print(
            f"  {c(C.GRAY, '»»')} "
            f"{c(C.YELLOW, 'accept edits OFF')} "
            f"{c(C.GRAY, '— destructive tools will prompt')}"
        )
    return DispatchResult.CONTINUE


def _handle_sandbox(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """`/sandbox` — print the active sandbox state for destructive shell tools."""
    from alpha import shell_sandbox as _sandbox

    print(f"  {c(C.CYAN, _sandbox.describe())}")
    cfg = _sandbox.load_config()
    if cfg.enabled:
        print(c(C.GRAY, f"  tool={cfg.tool}  deny_network={cfg.deny_network}"))
        if cfg.extra_args:
            print(c(C.GRAY, f"  extra_args={cfg.extra_args}"))
    else:
        print(
            c(
                C.GRAY,
                "  Configure under \"sandbox\" in .alpha/settings.json, "
                "or set ALPHA_SANDBOX=1 for the current session.",
            )
        )
    return DispatchResult.CONTINUE
