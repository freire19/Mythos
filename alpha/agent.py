"""
Core agent loop for Alpha Code.

Simplified autonomous engine: LLM call -> tool detection -> approval -> execution.
Includes intelligent context compression, token tracking, and smart loop detection.
"""

import json
import logging
from collections.abc import AsyncGenerator

from .approval import is_denied, needs_approval
from .config import MAX_ITERATIONS
from .context import (
    compress_until_under_budget,
    estimate_messages_tokens,
    get_context_limit,
    is_context_overflow_error,
    needs_compression,
)
from .executor import build_assistant_tool_message, execute_tool_calls
from .llm import stream_chat_with_tools

logger = logging.getLogger(__name__)

# ─── Loop detection (#DM037: extraido para _loop_detect.py) ───
from ._loop_detect import (
    _CYCLE_WINDOW,
    _LOOP_DETECT_MIN_CALLS,
    _LOOP_DETECT_MIN_ITER,
    _STALE_WINDOW,
    call_signature,
    detect_loop,
    result_preview,
)


async def run_agent(
    messages: list[dict],
    user_message: str,
    temperature: float = 0.5,
    provider: str = "deepseek",
    get_tool_fn=None,
    tools: list[dict] | None = None,
    approval_callback=None,
    max_iterations: int | None = None,
    workspace: str | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Run the agent loop. Async generator yielding display events.

    Features:
    - Intelligent context compression via LLM summarization
    - Token budget tracking per provider
    - Smart loop detection (exact, fuzzy, cycle, stale)

    Args:
        messages: Full conversation messages (system + history + new user msg).
        user_message: The current user message text.
        temperature: LLM temperature.
        provider: LLM provider name.
        get_tool_fn: Function(name) -> ToolDefinition for looking up tools.
        tools: OpenAI-format tool definitions list.
        approval_callback: Sync function(tool_name, args) -> bool for approval.
        max_iterations: Override iteration limit (defaults to MAX_ITERATIONS).

    Yields:
        {"type": "token", "text": "..."}
        {"type": "tool_call", "name": ..., "args": ...}
        {"type": "tool_result", "name": ..., "result": ...}
        {"type": "approval_needed", "name": ..., "args": ...}
        {"type": "context_compressed", "before": int, "after": int}
        {"type": "done", "reply": "full text"}
        {"type": "error", "message": "..."}
    """
    if tools is None:
        tools = []

    iteration_limit = max_iterations if max_iterations is not None else MAX_ITERATIONS
    full_response = ""

    # Track tool calls for smart loop detection
    _recent_calls: list[str] = []
    _recent_results: list[str] = []

    for iteration in range(iteration_limit):
        logger.info(f"Agent iteration {iteration + 1}/{iteration_limit}")

        # ── Pre-call adaptive compression ──
        if needs_compression(messages, provider):
            tokens_before = estimate_messages_tokens(messages)
            try:
                _, tokens_after = await compress_until_under_budget(
                    messages, provider, stream_chat_with_tools
                )
                if tokens_after != tokens_before:
                    yield {
                        "type": "context_compressed",
                        "before": tokens_before,
                        "after": tokens_after,
                    }
            except TimeoutError as e:
                # Compression chamou um LLM que estourou. Continua com
                # contexto inflado — o hard truncate fallback (#062) cobre
                # o caso onde isso vira loop. Em Python 3.11+ `asyncio.
                # TimeoutError` e alias de TimeoutError, entao um unico
                # handler ja cobre os dois caminhos sem precisar importar
                # asyncio (que nao e usado em mais nada neste modulo).
                logger.warning(
                    f"Context compression timeout: {e} — continuing without compression"
                )
            except Exception:
                # #053: bugs reais em context.py virariam silenciosos com
                # `except Exception as e: logger.warning(...)`. exc_info=True
                # preserva o frame onde o bug aconteceu para diagnostico,
                # sem derrubar o agent loop.
                logger.exception("Context compression failed unexpectedly — continuing")

        # ── Stream LLM call (with one overflow retry) ──
        final_event = None
        overflow_retried = False

        while True:
            final_event = None
            try:
                async for event in stream_chat_with_tools(
                    messages, tools, temperature, provider=provider
                ):
                    if event["type"] == "content_token":
                        yield {"type": "token", "text": event["token"]}
                    elif event["type"] == "stream_reset":
                        # llm.py vai retentar; tokens ja yieldados sao da
                        # tentativa abortada. Caller (REPL/main) pode limpar UI.
                        yield event
                    elif event["type"] == "final":
                        final_event = event
            except Exception as e:
                logger.exception(f"LLM stream raised {type(e).__name__}")
                yield {
                    "type": "error",
                    "message": f"LLM stream error: {type(e).__name__}: {e}",
                }
                return

            if final_event is None:
                yield {"type": "error", "message": "No response from LLM"}
                return

            err = final_event.get("error")
            if err and is_context_overflow_error(err) and not overflow_retried:
                overflow_retried = True
                logger.warning(
                    f"Context overflow from provider — re-compressing aggressively: {err}"
                )
                try:
                    limit = get_context_limit(provider)
                    tokens_before = estimate_messages_tokens(messages)
                    _, tokens_after = await compress_until_under_budget(
                        messages,
                        provider,
                        stream_chat_with_tools,
                        target_tokens=int(limit * 0.4),
                        max_passes=3,
                    )
                    yield {
                        "type": "context_compressed",
                        "before": tokens_before,
                        "after": tokens_after,
                    }
                except Exception as ce:
                    logger.error(f"Aggressive compression failed: {ce}")
                    yield {"type": "error", "message": err}
                    return
                continue  # retry the LLM call once

            break

        # LLM error (non-overflow, or overflow that survived the retry)
        if final_event.get("error"):
            yield {"type": "error", "message": final_event["error"]}
            return

        # Accumulate text
        if final_event.get("content"):
            full_response += final_event["content"]

        # No tool calls = final text response
        if not final_event.get("tool_calls"):
            yield {"type": "done", "reply": full_response}
            return

        # ── Smart loop detection ──
        call_sigs = [call_signature(tc) for tc in final_event["tool_calls"]]
        _recent_calls.extend(call_sigs)
        if len(_recent_calls) > _CYCLE_WINDOW * 3:
            _recent_calls[:] = _recent_calls[-_CYCLE_WINDOW * 3:]

        # Gate loop detection on accumulated tool calls, not iteration count.
        # A single iteration with a large parallel batch can fill _recent_calls
        # with 10+ entries before iteration 2, so a per-iteration threshold
        # (e.g. 3) misses loops that emerge across early batch-heavy turns.
        # AUDIT_V1.2 #018: gate by call count instead of iteration number.
        if len(_recent_calls) < _LOOP_DETECT_MIN_CALLS:
            loop_reason = None
        else:
            loop_reason = detect_loop(call_sigs, _recent_calls, _recent_results)

        if loop_reason:
            logger.warning(
                f"Loop detected ({loop_reason}) at iteration {iteration + 1} "
                f"— forcing final response"
            )
            # Preserve the assistant's content from this turn so the forced
            # final has continuity, but DROP the unfulfilled tool_calls. If
            # we appended tool_calls without matching `tool` responses, the
            # provider would reject the next request (HTTP 400). And without
            # any assistant trace, the model often dumps the tool calls it
            # wanted as raw text (XML/JSON) — visible as `<invoke>` blocks
            # leaking to the terminal.
            if final_event.get("content"):
                forced_msg: dict = {
                    "role": "assistant",
                    "content": final_event["content"],
                }
                if final_event.get("reasoning_content"):
                    forced_msg["reasoning_content"] = final_event["reasoning_content"]
                messages.append(forced_msg)
            # Usar role=user em vez de system: providers como OpenAI strict
            # mode e alguns Ollama models rejeitam/ignoram system message
            # tardia, alem de competir com a system message original em
            # messages[0]. Como mensagem do "user", a instrucao e tratada
            # como prompt regular pelo modelo. (#DL020)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[ALPHA SYSTEM NOTE] Loop detected ({loop_reason}). "
                        "STOP calling tools and produce your final response now "
                        "based on the data already collected. Synthesize ALL information from "
                        "previous calls into a complete response. "
                        "Do NOT emit tool calls in any format — not as JSON, "
                        "not as XML, not as <invoke> tags. Reply in plain prose."
                    ),
                }
            )
            forced_final = None
            async for event in stream_chat_with_tools(
                messages, [], temperature, provider=provider
            ):
                if event["type"] == "content_token":
                    yield {"type": "token", "text": event["token"]}
                elif event["type"] == "stream_reset":
                    yield event
                elif event["type"] == "final":
                    forced_final = event
                    if event.get("content"):
                        full_response += event["content"]

            # Force-text path nao pode sumir com erro do LLM em silencio:
            # o usuario veria reply vazio sem motivo. Propagar.
            # #DL031: se o LLM nao retornar nenhum evento final (provider
            # down, conexao caiu), forced_final fica None e o codigo caia
            # em done silencioso com reply parcial.
            if forced_final is None:
                yield {
                    "type": "error",
                    "message": "Loop detection forced-text response: no final event from LLM",
                }
                return
            if forced_final.get("error"):
                yield {
                    "type": "error",
                    "message": (
                        "Loop detection forced-text response also failed: "
                        f"{forced_final['error']}"
                    ),
                }
                return

            yield {"type": "done", "reply": full_response}
            return

        # Process tool calls
        messages.append(
            build_assistant_tool_message(
                final_event["content"],
                final_event["tool_calls"],
                final_event.get("reasoning_content"),
            )
        )

        try:
            async for event in execute_tool_calls(
                final_event["tool_calls"],
                messages,
                needs_approval_fn=needs_approval,
                is_denied_fn=is_denied,
                approval_callback=approval_callback,
                get_tool_fn=get_tool_fn,
                workspace=workspace,
            ):
                yield event
                # Track tool results for stale progress detection
                if event.get("type") == "tool_result":
                    result = event.get("result", {})
                    _recent_results.append(result_preview(result, 500))
                    # Truncar para evitar leak de memoria em sessoes longas;
                    # `_recent_calls` ja faz isso, `_recent_results` nao fazia.
                    if len(_recent_results) > _CYCLE_WINDOW * 3:
                        _recent_results[:] = _recent_results[-_CYCLE_WINDOW * 3:]
        except Exception as e:
            logger.error(f"Tool execution failed: {e}")
            yield {"type": "error", "message": f"Tool execution failed: {e}"}
            return
        finally:
            # If interrupted (Ctrl+C / CancelledError) mid-tool, the assistant
            # tool_calls may have no matching tool responses, which makes the
            # provider reject the next request with HTTP 400. Backfill missing
            # tool messages so the conversation stays well-formed.
            last_assistant = None
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    last_assistant = msg
                    break
            if last_assistant:
                responded = {
                    m.get("tool_call_id")
                    for m in messages
                    if m.get("role") == "tool"
                }
                for tc in last_assistant["tool_calls"]:
                    if tc["id"] not in responded:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps({"error": "interrupted"}),
                        })

    # Max iterations reached
    yield {
        "type": "token",
        "text": "\n\n[Maximum iterations reached]",
    }
    yield {"type": "done", "reply": full_response or "[Max iterations reached]"}
