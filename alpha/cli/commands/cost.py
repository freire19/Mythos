"""Cost/telemetry handlers: /cost, /stats, /preflight, /context."""

from __future__ import annotations

from alpha.cost import session_summary
from alpha.display import C, c
from alpha.stats import session_summary as stats_summary

from ._types import DispatchResult, ReplContext


def _handle_cost(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Show running token/cost totals for the current session."""
    s = session_summary()
    if s["calls"] == 0:
        print(f"  {c(C.GRAY, 'No LLM calls yet this session.')}")
        return DispatchResult.CONTINUE
    calls_str = f"{s['calls']} call(s)"
    in_str = f"{s['tokens_in']:,}"
    out_str = f"{s['tokens_out']:,}"
    cost_str = f"${s['cost_usd']:.4f}"
    print()
    print(f"  {c(C.VIOLET + C.BOLD, '┌─ COST — current session ─────────────────')}")
    print(
        f"  {c(C.VIOLET, '│')} "
        f"{c(C.WHITE + C.BOLD, calls_str)} — "
        f"{c(C.WHITE, in_str)} {c(C.GRAY, 'in')} / "
        f"{c(C.WHITE, out_str)} {c(C.GRAY, 'out')} — "
        f"{c(C.GREEN + C.BOLD, cost_str)}"
    )
    if s["by_model"] and len(s["by_model"]) > 1:
        print(f"  {c(C.VIOLET, '│')}")
        for row in s["by_model"]:
            label = f"{row['provider']}/{row['model']}"
            row_in = f"{row['tokens_in']:,}"
            row_out = f"{row['tokens_out']:,}"
            row_cost = f"${row['cost_usd']:.4f}"
            print(
                f"  {c(C.VIOLET, '│')} {c(C.GRAY, '·')} "
                f"{c(C.CYAN, label)}: "
                f"{row_in} in / {row_out} out — "
                f"{c(C.GREEN, row_cost)}"
            )
    print(f"  {c(C.VIOLET + C.BOLD, '└──────────────────────────────────────────')}")
    return DispatchResult.CONTINUE


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _handle_preflight(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Show analytics for pre_flight decisions logged across all sessions.

    Reads ~/.alpha/memory/preflight_feedback.jsonl (written by every
    approve/reject on a pre_flight card). Used to spot patterns the
    agent's estimator is consistently wrong about, validate budget
    caps are surviving real workflows, and decide whether slice 3
    self-tuning is worth implementing.
    """
    from alpha.preflight import summarize

    summary = summarize()
    print()
    print(f"  {c(C.VIOLET + C.BOLD, '┌─ PRE-FLIGHT — feedback log ──────────────')}")
    if summary["total"] == 0:
        print(
            f"  {c(C.VIOLET, '│')} "
            f"{c(C.GRAY, 'No pre_flight decisions recorded yet.')}"
        )
        print(
            f"  {c(C.VIOLET, '│')} "
            f"{c(C.GRAY, 'Cards are logged when the agent calls pre_flight and')}"
        )
        print(
            f"  {c(C.VIOLET, '│')} "
            f"{c(C.GRAY, 'you approve/reject. Run a destructive turn to start.')}"
        )
        print(f"  {c(C.VIOLET + C.BOLD, '└──────────────────────────────────────────')}")
        return DispatchResult.CONTINUE

    total = summary["total"]
    print(
        f"  {c(C.VIOLET, '│')} "
        f"{c(C.WHITE + C.BOLD, str(total))} {c(C.GRAY, 'decision(s) recorded')}"
    )

    # Decision breakdown
    decisions = summary["decisions"]
    if decisions:
        print(f"  {c(C.VIOLET, '│')}")
        print(f"  {c(C.VIOLET, '│')} {c(C.GRAY + C.BOLD, 'Decisions:')}")
        for decision in sorted(decisions, key=lambda d: -decisions[d]):
            count = decisions[decision]
            pct = 100.0 * count / total
            color = {
                "approve": C.GREEN,
                "approve_all": C.GREEN,
                "reject": C.RED,
                "interrupt": C.RED_DARK,
                "eof": C.GRAY,
            }.get(decision, C.WHITE)
            bar = "█" * max(1, int(pct / 5))  # ~5% per block
            print(
                f"  {c(C.VIOLET, '│')}   "
                f"{c(color, decision):<14} "
                f"{c(C.WHITE, f'{count:>4}')} {c(C.GRAY, f'({pct:5.1f}%)')}  "
                f"{c(color, bar)}"
            )

    # Cost stats
    avg_cost = summary["avg_estimated_cost_usd"]
    total_cost = summary["total_estimated_cost_usd"]
    if avg_cost > 0:
        print(f"  {c(C.VIOLET, '│')}")
        print(
            f"  {c(C.VIOLET, '│')} "
            f"{c(C.GRAY, 'avg estimated:')} {c(C.WHITE, f'${avg_cost:.4f}')}  "
            f"{c(C.GRAY, 'lifetime estimated:')} {c(C.GREEN + C.BOLD, f'${total_cost:.4f}')}"
        )

    # Top tools (most-planned)
    by_tool = summary["by_tool"]
    if by_tool:
        top = sorted(
            by_tool.items(),
            key=lambda kv: -sum(kv[1].values()),
        )[:5]
        print(f"  {c(C.VIOLET, '│')}")
        print(f"  {c(C.VIOLET, '│')} {c(C.GRAY + C.BOLD, 'Top tools in plans:')}")
        for tool, decisions_for_tool in top:
            count = sum(decisions_for_tool.values())
            approve_rate = (
                100.0
                * (decisions_for_tool.get("approve", 0) + decisions_for_tool.get("approve_all", 0))
                / count
                if count else 0.0
            )
            print(
                f"  {c(C.VIOLET, '│')}   "
                f"{c(C.CYAN, tool):<28} "
                f"{c(C.WHITE, f'{count:>4}')} {c(C.GRAY, 'plan(s)')}  "
                f"{c(C.GRAY, 'approve rate:')} {c(C.WHITE, f'{approve_rate:>5.1f}%')}"
            )

    print(f"  {c(C.VIOLET + C.BOLD, '└──────────────────────────────────────────')}")
    return DispatchResult.CONTINUE


def _handle_stats(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Show session telemetry: iterations, tool usage, latency, approvals."""
    s = stats_summary()
    cost = session_summary()
    print()
    print(f"  {c(C.VIOLET + C.BOLD, '┌─ STATS — current session ────────────────')}")
    print(
        f"  {c(C.VIOLET, '│')} "
        f"{c(C.GRAY, 'uptime:')} {c(C.WHITE, _fmt_duration(s['uptime_s']))}  "
        f"{c(C.GRAY, 'iterations:')} {c(C.WHITE, str(s['iterations']))}  "
        f"{c(C.GRAY, 'tool calls:')} {c(C.WHITE, str(s['tool_calls_total']))}"
    )
    if cost["calls"]:
        avg_in = cost["tokens_in"] // cost["calls"]
        avg_out = cost["tokens_out"] // cost["calls"]
        print(
            f"  {c(C.VIOLET, '│')} "
            f"{c(C.GRAY, 'tokens/turn (avg):')} {c(C.WHITE, f'{avg_in:,}')} {c(C.GRAY, 'in')} / "
            f"{c(C.WHITE, f'{avg_out:,}')} {c(C.GRAY, 'out')}"
        )
    if s["approvals_required"]:
        rate = 100.0 * s["approvals_granted"] / s["approvals_required"]
        print(
            f"  {c(C.VIOLET, '│')} "
            f"{c(C.GRAY, 'approvals:')} "
            f"{c(C.WHITE, str(s['approvals_granted']))}/"
            f"{c(C.WHITE, str(s['approvals_required']))} granted "
            f"{c(C.GRAY, f'({rate:.0f}%)')}"
        )
    if s["tools"]:
        print(f"  {c(C.VIOLET, '│')}")
        print(f"  {c(C.VIOLET, '│')} {c(C.GRAY + C.BOLD, 'Top tools:')}")
        for t in s["tools"][:5]:
            calls_col = f"{t['calls']:>3}"
            avg_col = f"{t['avg_ms']:>6.1f}ms"
            print(
                f"  {c(C.VIOLET, '│')}   "
                f"{c(C.CYAN, t['name']):<28} "
                f"{c(C.WHITE, calls_col)} {c(C.GRAY, 'calls')} "
                f"{c(C.GRAY, 'avg')} {c(C.WHITE, avg_col)}"
            )
    print(f"  {c(C.VIOLET + C.BOLD, '└──────────────────────────────────────────')}")
    return DispatchResult.CONTINUE


def _handle_context(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Show current context window usage."""
    from alpha.context import (
        COMPRESSION_THRESHOLD,
        MAX_MESSAGES,
        estimate_messages_tokens,
        get_context_limit,
    )

    used = estimate_messages_tokens(ctx.messages)
    limit = get_context_limit(ctx.provider)
    pct = (used / limit * 100) if limit else 0.0
    trigger_at = int(limit * COMPRESSION_THRESHOLD)

    if pct >= 90:
        bar_color = C.RED + C.BOLD
    elif pct >= 70:
        bar_color = C.YELLOW + C.BOLD
    elif pct >= 50:
        bar_color = C.YELLOW
    else:
        bar_color = C.GREEN

    bar_len = 30
    filled = min(bar_len, int(pct / 100 * bar_len))
    if pct > 0 and filled == 0:
        filled = 1
    bar = c(bar_color, "█" * filled) + c(C.GRAY_DARK, "░" * (bar_len - filled))

    print(f"  {c(C.CYAN, 'Provider:')}    {ctx.provider} ({ctx.cfg.get('model', '?')})")
    print(f"  {c(C.CYAN, 'Tokens:')}      {used:,} / {limit:,} ({pct:.1f}%)")
    print(f"  {c(C.CYAN, 'Usage:')}       {bar}")
    print(f"  {c(C.CYAN, 'Messages:')}    {len(ctx.messages)} / {MAX_MESSAGES}")
    print(
        f"  {c(C.CYAN, 'Compresses:')}  at {int(COMPRESSION_THRESHOLD * 100)}% "
        f"({trigger_at:,} tokens) or {MAX_MESSAGES} messages"
    )
    return DispatchResult.CONTINUE
