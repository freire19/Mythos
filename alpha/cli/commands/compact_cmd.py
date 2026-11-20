"""`/compact [foco]` — compress the conversation now, optionally focused.

The automatic compressor (`agent._maybe_compress`) only fires at 70% of
the context window. `/compact` runs the same summarizer on demand with a
tiny protected tail, so the next turn starts from `system + summary +
last exchange`. Extra words become instructions for the summarizer
("/compact mantenha os caminhos de arquivo e as decisões de API").
`/save` persists the compacted list; `/history` still shows the raw log.
"""

from __future__ import annotations

import asyncio

from alpha.display import C, c, print_error

from ._types import DispatchResult, ReplContext

# Keep only the last user/assistant exchange verbatim. The range finder
# still refuses to split an assistant.tool_calls from its tool results.
_MANUAL_TAIL = 2


def _handle_compact(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    from alpha import hooks, llm
    from alpha.context import compress_context, estimate_messages_tokens, find_compressible_range

    focus = " ".join(parts[1:]).strip()
    start, end = find_compressible_range(ctx.messages, _MANUAL_TAIL)
    if start >= end:
        print(f"  {c(C.GRAY, 'Nada para compactar — a conversa ainda é curta.')}")
        return DispatchResult.CONTINUE

    before_tokens = estimate_messages_tokens(ctx.messages)
    before_msgs = len(ctx.messages)
    if hooks.has_event("pre_compact"):
        try:
            hooks.fire(
                "pre_compact",
                extra={"tokens_before": before_tokens, "messages_count": before_msgs, "manual": True},
            )
        except Exception:
            pass

    label = f" (foco: {focus[:60]})" if focus else ""
    print(f"  {c(C.GRAY, f'Compactando {end - start} mensagens{label}…')}")
    try:
        asyncio.run(
            compress_context(
                ctx.messages, ctx.provider, llm.stream_chat_with_tools,
                protected_tail=_MANUAL_TAIL, instructions=focus,
            )
        )
    except Exception as e:
        print_error(f"/compact falhou: {type(e).__name__}: {e}")
        return DispatchResult.CONTINUE

    after_tokens = estimate_messages_tokens(ctx.messages)
    if len(ctx.messages) >= before_msgs:
        print(f"  {c(C.YELLOW, '⚠')} O sumarizador não devolveu nada; contexto mantido.")
        return DispatchResult.CONTINUE
    pct = (after_tokens / before_tokens - 1) * 100 if before_tokens else 0.0
    delta = f"{pct:+.0f}%"  # "-62%" normally; "+34%" when a tiny chat grows
    print(
        f"  {c(C.GREEN, '✦')} {c(C.CYAN, '/compact')} "
        f"{c(C.GRAY, f'— {before_msgs} → {len(ctx.messages)} mensagens, {before_tokens:,} → {after_tokens:,} tokens ({delta})')}"
    )
    return DispatchResult.CONTINUE
