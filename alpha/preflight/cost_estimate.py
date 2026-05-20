"""Heuristic cost estimator for a planned tool call.

Math is intentionally crude — chars/4 token approximation, fixed expected-
output sizes per tool family. Goal is a useful ballpark on the approval
card, not an invoice. The real LLM cost comes from `alpha/cost.py:_PRICING`
which we read directly so estimates track price changes automatically.

Future (out of scope): pass actual `messages` through the provider's
tokenizer when `ALPHA_ACCURATE_COST_ESTIMATE=1`.
"""

from __future__ import annotations

from ..context import estimate_tokens
from ..cost import _price_for


# Per-tool expected token output. Conservative — better to over-estimate
# cost than surprise the user with an under-estimate.
_EXPECTED_OUTPUT_TOKENS = {
    # Tool calls themselves don't generate LLM output, but they trigger a
    # follow-up LLM turn whose response we have to count. These numbers
    # approximate that follow-up's completion size.
    "read_file": 200,
    "write_file": 100,
    "edit_file": 150,
    "execute_shell": 400,
    "execute_python": 400,
    "execute_pipeline": 400,
    "grep_files": 300,
    "list_files": 250,
    "search_files": 300,
    "git_status": 200,
    "git_diff": 500,
    "git_log": 400,
    "git_commit": 100,
    "delegate_task": 1500,    # sub-agent loops are pricier
    "delegate_parallel": 3000,
    "delegate_consensus": 2500,
}

_DEFAULT_OUTPUT_TOKENS = 250


def estimate_step_cost(
    tool: str,
    args_preview: str,
    model: str,
) -> float:
    """USD estimate for one planned tool call's NEXT LLM turn.

    A tool call by itself costs nothing — the cost is the follow-up
    LLM turn that processes the tool result. We estimate input as the
    args + an expected result preview, output as the per-tool-family
    constant above.
    """
    p_in, p_out = _price_for(model)
    if p_in == 0.0 and p_out == 0.0:
        return 0.0  # unknown model — surface as `~$?` on the card

    # Args land in the prompt of the next turn. Tool result also lands
    # in the prompt — assume a result preview roughly twice the args
    # size (lookups return more than they take in, on average).
    in_tokens = estimate_tokens(args_preview) * 3

    out_tokens = _EXPECTED_OUTPUT_TOKENS.get(tool, _DEFAULT_OUTPUT_TOKENS)

    return (in_tokens * p_in + out_tokens * p_out) / 1_000_000


def estimate_total_cost(steps: list[dict], model: str) -> float:
    """Sum of per-step estimates for a planned batch."""
    return sum(
        estimate_step_cost(
            tool=str(step.get("tool", "")),
            args_preview=str(step.get("args_preview", "")),
            model=model,
        )
        for step in steps
    )
