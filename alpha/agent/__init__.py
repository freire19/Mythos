"""
Core agent loop for Mythos.

Simplified autonomous engine: LLM call -> tool detection -> approval -> execution.
Includes intelligent context compression, token tracking, and smart loop detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

from ..approval import is_denied, needs_approval
from ..config import DEFAULT_PROVIDER, LLM_TIMEOUT, LOOP_DETECTION, MAX_ITERATIONS, MAX_TURN_SECONDS, get_provider_config  # noqa: F401 — LOOP_DETECTION re-exported for back-compat
from ..cost import record_usage, session_cost_usd
from .._turn_budget import clamp as _clamp_turn_budget, remaining as _turn_budget_remaining
from ..preflight import env_float, estimate_total_cost
from ..stats import record_iteration

import os as _os


def _record_session_path() -> str | None:
    """Read ALPHA_RECORD_SESSION_PATH at call time so tests using the
    standard `monkeypatch.setenv` idiom actually take effect."""
    return _os.environ.get("ALPHA_RECORD_SESSION_PATH")
from ..context import (
    compress_until_under_budget,
    estimate_messages_tokens,
    get_context_limit,
    is_context_overflow_error,
    needs_compression,
    record_real_token_usage,
)
from ..executor import build_assistant_tool_message, execute_tool_calls
from ..llm import stream_chat_with_tools
from ..tools import ToolCategory, ToolSafety
from .loop import (
    _call_signature,
    _CYCLE_WINDOW,
    _detect_loop,
    _LOOP_DETECT_MIN_CALLS,
    _result_is_failure,
    _result_preview,
)

logger = logging.getLogger(__name__)

# #DM075: cap compartilhado das 3 janelas de loop detection
# (_recent_calls/_recent_results/_recent_failures) — elas precisam truncar em
# lockstep; o `_CYCLE_WINDOW * 3` inline em 5 sites permitia dessincronizar.
_RECENT_WINDOW_CAP = _CYCLE_WINDOW * 3


def _llm_budget_timeout() -> float | None:
    """Compute the per-call LLM timeout for the current turn budget.

    Returns None when no turn deadline is set (no clamp), else the
    seconds-left clamped to ``LLM_TIMEOUT``. Single source of truth for
    the 3 LLM-streaming call sites in this module (stream_one_turn,
    _maybe_compress, _produce_forced_response).
    """
    budget = _clamp_turn_budget(float(LLM_TIMEOUT))
    no_deadline = _turn_budget_remaining() == float("inf")
    return None if no_deadline else budget


def _llm_budget_timeout_events(
    anomaly_reason: str,
    anomaly_label: str,
    full_response: str,
    timeout_arg: float | None,
) -> tuple[dict, dict, str]:
    """Build the (anomaly, done) events + log message yielded when an LLM
    call exceeds the remaining turn budget. Only called from the
    ``except TimeoutError`` handlers, so the dict/f-string work never
    runs on the happy path.
    """
    budget = (
        timeout_arg
        if timeout_arg is not None
        else _clamp_turn_budget(float(LLM_TIMEOUT))
    )
    anomaly_event = {
        "type": "anomaly",
        "reason": anomaly_reason,
        "detail": (
            f"{anomaly_label} excedeu o tempo restante do turno "
            f"({budget:.0f}s)"
        ),
    }
    done_event = {"type": "done", "reply": full_response}
    log_msg = f"{anomaly_label} timed out by turn budget ({budget:.1f}s)"
    return anomaly_event, done_event, log_msg


def _llm_budget_clamp_setup(
    anomaly_reason: str,
    anomaly_label: str,
    full_response: str,
) -> tuple[float | None, dict, dict, str]:
    """Eager composition of `_llm_budget_timeout` + `_llm_budget_timeout_events`.

    Returned tuple: ``(timeout_arg, anomaly_event, done_event, log_msg)``
    — the contract extracted in CODE_REVIEW4 #1. Kept for external
    callers/tests; the agent loop itself uses the two lazy halves.
    """
    timeout_arg = _llm_budget_timeout()
    anomaly_event, done_event, log_msg = _llm_budget_timeout_events(
        anomaly_reason, anomaly_label, full_response, timeout_arg
    )
    return timeout_arg, anomaly_event, done_event, log_msg


# Anomaly `reason` tags. Single source of truth for the strings this loop
# emits, so consumers (display, tests) key off the SAME values the loop
# yields instead of duplicating the literals across modules.
REASON_MAX_TURN_SECONDS = "max_turn_seconds"
REASON_MAX_ITERATIONS = "max_iterations"
REASON_LLM_TIMEOUT_BY_BUDGET = "llm_timeout_by_budget"
REASON_COMPRESSION_TIMEOUT_BY_BUDGET = "compression_timeout_by_budget"
REASON_FORCED_RESPONSE_TIMEOUT_BY_BUDGET = "forced_response_timeout_by_budget"


# Pre-flight enforcement thresholds (Plano-v3 RFC slice 2). Module-scope
# so tests can monkeypatch without reaching into closures.
PREFLIGHT_REPROMPT_LIMIT = 1
PREFLIGHT_REQUIRED_DESTRUCTIVE_COUNT = 2


def _count_destructive_non_planning(
    tool_calls: list[dict],
    get_tool_fn,
) -> int:
    """Count tool calls that are DESTRUCTIVE and not in the PLANNING
    category. Planning tools (pre_flight, present_plan, todo_write) are
    DESTRUCTIVE only to force the approval gate — they don't mutate
    state or cost real money, so they don't trigger the re-prompt rule.
    """
    n = 0
    for tc in tool_calls:
        name = tc.get("name", "")
        try:
            td = get_tool_fn(name)
        except Exception:
            td = None
        if td is None or td.category == ToolCategory.PLANNING:
            continue
        if td.safety == ToolSafety.DESTRUCTIVE:
            n += 1
    return n


# ─── run_agent helpers ───
#
# Four async-generator helpers so the main loop reads as:
# compress → stream → react to loop/preflight → execute.


async def _maybe_compress(messages: list[dict], provider: str) -> AsyncGenerator[dict, None]:
    """Pre-call adaptive compression. Yields context_compressed event when
    tokens actually shrank. Failure logged but never raised — agent keeps
    going with inflated context; the overflow retry path will handle
    blowups."""
    if not needs_compression(messages, provider):
        return
    tokens_before = estimate_messages_tokens(messages)
    # pre_compact: última chance de snapshotar o histórico antes do
    # sumarizador reescrevê-lo. has_event evita o to_thread no caso comum.
    from .. import hooks as _hooks
    if _hooks.has_event("pre_compact"):
        try:
            await asyncio.to_thread(
                _hooks.fire,
                "pre_compact",
                extra={
                    "tokens_before": tokens_before,
                    "messages_count": len(messages),
                },
            )
        except Exception:
            logger.debug("pre_compact hook failed (non-fatal)", exc_info=True)
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
        # Compression chamou um LLM que estourou. Em Python 3.11+
        # `asyncio.TimeoutError` e alias de TimeoutError — um handler cobre
        # os dois. Continua com contexto inflado.
        logger.warning("Context compression timeout: %s — continuing without compression", e)
    except Exception:
        # #053: bugs reais em context.py virariam silenciosos com
        # `except Exception as e: logger.warning(...)`. exc_info preserva
        # o frame para diagnostico sem derrubar o agent loop.
        logger.exception("Context compression failed unexpectedly — continuing")


async def _handle_overflow_retry(
    messages: list[dict], provider: str
) -> AsyncGenerator[dict, None]:
    """Aggressive re-compression after the provider rejected the request
    with a context-overflow error. Targets 40% of the limit and runs up
    to 3 passes. Falls back to hard-truncate if compression itself fails.
    Yields the context_compressed event when shrinkage happened."""
    shrank = False
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
        if tokens_after < tokens_before:
            shrank = True
            yield {
                "type": "context_compressed",
                "before": tokens_before,
                "after": tokens_after,
            }
    except Exception as ce:
        logger.exception("Aggressive compression failed: %s", ce)
        from ..context import find_compressible_range, hard_truncate
        start, end = find_compressible_range(messages)
        if start < end:
            messages[:] = hard_truncate(messages, start, end)
            shrank = True
    # #DL-002: if neither compression nor hard-truncate shrank anything (the
    # estimate undercounted, or nothing is compressible and start >= end), the
    # imminent retry will hit the SAME overflow. Without a signal the UI goes
    # silent through a doomed retry and the dead-end is invisible in logs.
    if not shrank:
        logger.warning(
            "Overflow recovery could not shrink the context — retry will "
            "likely fail the same way"
        )
        yield {
            "type": "anomaly",
            "reason": "context_overflow_unrecoverable",
            "detail": (
                "Estouro de contexto sem nada compressível/truncável — "
                "a re-tentativa provavelmente falhará igual."
            ),
        }


async def _stream_one_turn(
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    provider: str,
    provider_model: str,
) -> AsyncGenerator[dict, None]:
    """One LLM call with the in-band overflow retry.

    Yields user-visible events (`token`, `stream_reset`, `context_compressed`)
    and, on completion, exactly one ``{"type": "_final", "event": ...}``
    marker carrying the provider's final event for the caller to react to.
    Marker shape isn't part of the public event vocabulary — it's an
    internal handoff.
    """
    overflow_retried = False
    while True:
        final_event = None
        record_path = _record_session_path()
        # Buffer the turn's events when ALPHA_RECORD_SESSION_PATH is set
        # so we can append a complete turn (tokens + final) to the
        # session fixture on `final`. No-op otherwise.
        turn_buffer: list[dict] | None = [] if record_path else None
        async for event in stream_chat_with_tools(
            messages, tools, temperature, provider=provider
        ):
            if turn_buffer is not None:
                turn_buffer.append(event)
            if event["type"] == "content_token":
                yield {"type": "token", "text": event["token"]}
            elif event["type"] == "thinking_token":
                yield {"type": "thinking", "text": event["token"]}
            elif event["type"] == "stream_reset":
                # llm.py vai retentar; tokens ja yieldados sao da
                # tentativa abortada. Caller (REPL/main) pode limpar UI.
                yield event
            elif event["type"] == "final":
                final_event = event

        if turn_buffer is not None and record_path:
            try:
                from ..llm_fixtures import append_turn
                append_turn(
                    record_path,
                    turn_buffer,
                    scenario="session-record",
                    provider=provider,
                    model=provider_model,
                )
            except Exception as e:
                logger.debug("session recording failed (non-fatal): %s", e)

        if final_event is None:
            yield {"type": "_final", "event": None}
            return

        try:
            record_usage(
                provider=provider,
                model=provider_model,
                usage=final_event.get("usage"),
            )
        except Exception as e:
            logger.debug("cost tracking failed (non-fatal): %s", e)

        # Calibra a estimativa de contexto contra os tokens reais deste turno.
        # `messages` aqui é exatamente o que foi enviado (a resposta do
        # assistant só é anexada pelo caller depois deste retorno).
        try:
            record_real_token_usage(
                estimate_messages_tokens(messages),
                final_event.get("usage"),
            )
        except Exception as e:
            logger.debug("token calibration failed (non-fatal): %s", e)

        err = final_event.get("error")
        if err and is_context_overflow_error(err) and not overflow_retried:
            overflow_retried = True
            logger.warning(
                "Context overflow from provider — re-compressing aggressively: %s", err
            )
            async for evt in _handle_overflow_retry(messages, provider):
                yield evt
            continue  # retry the LLM call once

        yield {"type": "_final", "event": final_event}
        return


# Shared instruction tail for every "stop and summarize" nudge. One source so
# the loop-detection path and the cap paths can't drift in wording.
_FORCED_SUMMARY_INSTRUCTION = (
    "STOP calling tools and produce your final response now, synthesizing ALL "
    "information gathered from previous calls into a complete answer. Do NOT "
    "emit tool calls in any format — not as JSON, not as XML, not as <invoke> "
    "tags. Reply in plain prose."
)


async def _stream_forced_summary(
    messages: list[dict],
    temperature: float,
    provider: str,
    provider_model: str,
    full_response: str,
    nudge: str,
    anomaly_reason: str,
    anomaly_detail: str,
    error_label: str,
) -> AsyncGenerator[dict, None]:
    """Shared terminal path for every turn that ends WITHOUT the model
    volunteering a final answer — loop detection (`_produce_forced_response`)
    and the resource caps (`_force_final_summary`: max iterations / wall-clock).

    Appends `nudge` as a user message, streams ONE no-tools completion — with a
    single in-band context-overflow retry, exactly like `_stream_one_turn`, so a
    turn that hit a cap with a near-full context can still summarize instead of
    erroring out — then emits the terminating `anomaly` + `done`.

    LLM errors are PROPAGATED, never swallowed: an empty `done` would leave the
    user staring at a blank reply with no reason. This is the invariant the two
    callers previously implemented inconsistently (the cap path swallowed
    errors), now centralized so they can't diverge again."""
    messages.append({"role": "user", "content": nudge})

    forced_final = None
    overflow_retried = False
    while True:
        forced_final = None
        async for event in stream_chat_with_tools(
            messages, [], temperature, provider=provider
        ):
            if event["type"] == "content_token":
                yield {"type": "token", "text": event["token"]}
            elif event["type"] == "thinking_token":
                yield {"type": "thinking", "text": event["token"]}
            elif event["type"] == "stream_reset":
                yield event
            elif event["type"] == "final":
                forced_final = event
        err = forced_final.get("error") if forced_final else None
        if err and is_context_overflow_error(err) and not overflow_retried:
            overflow_retried = True
            logger.warning(
                "Context overflow during forced summary — re-compressing: %s", err
            )
            async for evt in _handle_overflow_retry(messages, provider):
                yield evt
            continue  # retry the no-tools call once on a compressed history
        break

    if forced_final is not None:
        if forced_final.get("content"):
            full_response += forced_final["content"]
        try:
            record_usage(
                provider=provider,
                model=provider_model,
                usage=forced_final.get("usage"),
            )
        except Exception as e:
            logger.debug("cost tracking failed (non-fatal): %s", e)

    # DL031: forced_final is None when the LLM never emitted `final` (provider
    # timeout / dropped connection). Either failure mode is surfaced, not hidden.
    if forced_final is None:
        yield {
            "type": "error",
            "message": f"{error_label}: no final event from LLM (provider may be down)",
        }
        return
    if forced_final.get("error"):
        yield {
            "type": "error",
            "message": f"{error_label}: {forced_final['error']}",
        }
        return

    yield {"type": "anomaly", "reason": anomaly_reason, "detail": anomaly_detail}
    yield {"type": "done", "reply": full_response}


async def _produce_forced_response(
    messages: list[dict],
    temperature: float,
    provider: str,
    provider_model: str,
    loop_reason: str,
    final_event: dict,
    full_response: str,
) -> AsyncGenerator[dict, None]:
    """Loop-detected path: drop the assistant's tool_calls, append a
    user-role nudge, stream one more LLM turn with no tools, and yield
    `done` or `error`. ``full_response`` is the accumulated text so far —
    we append the forced turn's content before emitting done."""
    # Preserve the assistant's content from this turn so the forced
    # final has continuity, but DROP the unfulfilled tool_calls. If we
    # appended tool_calls without matching `tool` responses, the provider
    # would reject the next request (HTTP 400). And without any assistant
    # trace, the model often dumps the tool calls it wanted as raw text
    # (XML/JSON) — visible as `<invoke>` blocks leaking to the terminal.
    if final_event.get("content"):
        forced_msg: dict = {
            "role": "assistant",
            "content": final_event["content"],
        }
        if final_event.get("reasoning_content"):
            forced_msg["reasoning_content"] = final_event["reasoning_content"]
        messages.append(forced_msg)
    # Usar role=user em vez de system: providers como OpenAI strict mode
    # e alguns Ollama models rejeitam/ignoram system message tardia, alem
    # de competir com a system message original em messages[0]. (#DL020)
    # The nudge append + stream + finalize is shared with the cap paths.
    nudge = (
        f"[ALPHA SYSTEM NOTE] Loop detected ({loop_reason}). "
        + _FORCED_SUMMARY_INSTRUCTION
    )
    async for event in _stream_forced_summary(
        messages, temperature, provider, provider_model, full_response,
        nudge,
        "loop_detected",
        f"loop detectado ({loop_reason}) — resposta forçada sem mais tools",
        "Loop detection forced-text response",
    ):
        yield event


async def _force_final_summary(
    messages: list[dict],
    temperature: float,
    provider: str,
    provider_model: str,
    full_response: str,
    anomaly_reason: str,
    anomaly_detail: str,
) -> AsyncGenerator[dict, None]:
    """Cap-reached path (max iterations / wall-clock budget): the model kept
    calling tools and never volunteered a final answer, so force one no-tools
    summary — the user always gets a response synthesizing the work done.

    Thin wrapper over `_stream_forced_summary`. Unlike `_produce_forced_response`
    there is no pending `final_event` to unwind: the loop already appended the
    assistant tool message and its matching tool results, so `messages` is in a
    clean state for one more completion."""
    nudge = (
        "[ALPHA SYSTEM NOTE] You reached this turn's work limit "
        f"({anomaly_detail}). " + _FORCED_SUMMARY_INSTRUCTION
    )
    try:
        async with asyncio.timeout(float(LLM_TIMEOUT)):
            async for event in _stream_forced_summary(
                messages, temperature, provider, provider_model, full_response,
                nudge, anomaly_reason, anomaly_detail,
                "Forced summary (turn cap)",
            ):
                yield event
    except TimeoutError:
        logger.warning(
            "Forced summary (turn cap) timed out after %ss", LLM_TIMEOUT
        )
        yield {"type": "anomaly", "reason": anomaly_reason, "detail": anomaly_detail}
        yield {"type": "done", "reply": full_response}


def _backfill_interrupted_tool_responses(messages: list[dict]) -> None:
    """Cleanup after CancelledError mid-tool: ensure each assistant
    tool_call has a matching tool response, otherwise the provider rejects
    the next request with HTTP 400. Appends synthetic `{"error":
    "interrupted"}` for any unmatched tc."""
    last_assistant = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_assistant = msg
            break
    if last_assistant is None:
        return
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


def _handle_dsml_drift(
    final_event: dict,
    messages: list[dict],
    dsml_failures: int,
    full_response: str,
) -> tuple[str, int, list[dict]]:
    """Decide what to do with a possible DSML-drift response and mutate
    `messages` accordingly. Extracted from run_agent's loop body (AUDIT_V2.2
    #020) so the orchestrator stays readable and this branch is unit-testable.

    A "drift" response is one where the provider emitted tool calls as
    `<invoke name="...">` markup inside the content field (llm.py sets
    `dsml_recovery_failed=True`) instead of the structured `tool_calls` field.

    Returns ``(action, new_dsml_failures, events)`` where action is:
    - ``"none"``     — not a drift response; caller proceeds to the error gate.
    - ``"reprompt"`` — a corrective note was appended to `messages`; caller
      should log and ``continue`` to the next iteration.
    - ``"abort"``    — unrecoverable after 2 attempts; caller yields ``events``
      (an anomaly + done) and ``return``s.
    """
    if not final_event.get("dsml_recovery_failed"):
        return ("none", dsml_failures, [])

    dsml_failures += 1
    if dsml_failures >= 2:
        logger.warning(
            "DSML drift unrecoverable after %d attempts — aborting turn",
            dsml_failures,
        )
        events = [
            {
                "type": "anomaly",
                "reason": "dsml_unrecoverable",
                "detail": (
                    f"modelo emitiu <invoke> markup {dsml_failures}x sem "
                    "recuperação — verifique provider ou troque modelo"
                ),
            },
            {"type": "done", "reply": full_response},
        ]
        return ("abort", dsml_failures, events)

    # Preserve the offending content as an assistant message so the provider
    # sees what it just said, then append a corrective user note. Same pattern
    # as loop detection / pre-flight reprompt.
    if final_event.get("content"):
        messages.append({
            "role": "assistant",
            "content": final_event["content"],
        })
    messages.append({
        "role": "user",
        "content": (
            "[ALPHA SYSTEM NOTE] You emitted tool calls as "
            "`<invoke name=\"...\">` markup inside the content "
            "field. That format is NOT executed — Alpha only "
            "runs tool calls submitted via the OpenAI-compatible "
            "`tool_calls` field on the assistant message. "
            "Please re-emit your intended tool calls through the "
            "structured tool_calls field. If you wanted to "
            "respond in plain prose, just write the prose with "
            "no <invoke>/<parameter>/<tool_calls> tags."
        ),
    })
    return ("reprompt", dsml_failures, [])


# AUDIT_V2.3 #503: helpers extraídos do corpo do run_agent (~340 linhas com
# ~10 responsabilidades sequenciais). Cada um cobre um bloco que era inline;
# mutam as listas por referência e devolvem o evento/valor para o caller
# fazer o yield — o loop mantém só a costura.

_ERROR_STREAK_NUDGE = (
    "[ALPHA SYSTEM NOTE] {loop_reason}. Your last tool calls "
    "were NOT executed. Stop and re-read the error messages "
    "above — do not retry the same approach. Either (a) fix "
    "the root cause the errors point at, (b) take a different "
    "path that avoids the failing dependency, or (c) if the "
    "environment truly can't support the task, say so and "
    "finish with what you have. Note: a non-zero exit code is "
    "not always an error (grep without matches returns 1)."
)


def _drain_bg_notifications(messages: list[dict]) -> dict | None:
    """Injeta notificações de bg tasks terminadas no topo da iteração
    (messages em estado limpo — nunca entre um assistant tool_calls e seus
    tool results). Retorna o anomaly event a emitir, ou None."""
    try:
        from ..bg_tasks import drain_finished_unnotified, format_notification
        finished = drain_finished_unnotified()
        if not finished:
            return None
        messages.append({
            "role": "user",
            "content": format_notification(finished),
        })
        try:
            from ..notify import background_finished
            background_finished(
                ", ".join(f"task #{t['task_id']}" for t in finished)
            )
        except Exception:
            logger.debug("bg finish notification skipped", exc_info=True)
        return {
            "type": "anomaly",
            "reason": "bg_task_finished",
            "detail": ", ".join(
                f"task #{t['task_id']} ({'ok' if t.get('exit_code') == 0 else 'exit ' + str(t.get('exit_code'))})"
                for t in finished
            ),
        }
    except Exception:
        logger.exception("bg task notification failed")
        return None


def _drain_bg_inbox(messages: list[dict], assistant_content: str | None = None) -> dict | None:
    """Background sub-agent CHILD only (`ALPHA_BG_INBOX` set by the parent):
    pull messages the parent posted while we run and inject them as a user
    note. `assistant_content` is the reply the model just produced when we
    are called at the stop point — it must enter the history first so the
    note never splits a turn. Returns the anomaly event to emit, or None."""
    try:
        from ..bg_resume import child_inbox, drain_inbox, format_inbox_note
        inbox = child_inbox()
        if inbox is None:
            return None
        texts = drain_inbox(inbox)
        if not texts:
            return None
        if assistant_content and assistant_content.strip():
            messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": format_inbox_note(texts)})
        return {
            "type": "anomaly",
            "reason": "bg_inbox_message",
            "detail": f"{len(texts)} message(s) from the parent agent",
        }
    except Exception:
        logger.exception("bg inbox drain failed")
        return None


def _check_wall_clock_cap(
    turn_start: float, budget_seconds: float, iteration: int
) -> float | None:
    """Retorna o elapsed (para a mensagem do caller) se o orçamento de
    wall-clock do turno estourou; None enquanto há tempo. Stops cascading
    retries (DSML drift + loop detector + tool timeout) from chaining into
    10+ minute turns."""
    elapsed = time.monotonic() - turn_start
    if elapsed < budget_seconds:
        return None
    logger.warning(
        "Turn wall-clock budget exceeded (%.1fs >= %ss) at iteration %d",
        elapsed, budget_seconds, iteration + 1,
    )
    return elapsed


def session_cost_cap() -> float | None:
    """Parse único do cap de sessão (ALPHA_MAX_SESSION_COST_USD), via
    `env_float` (strip + finitude + clamp de negativo). Consumido por
    `_session_cap_breach` (loop) e `_session_cap_skip` (delegate_tools)
    para os dois gates nunca divergirem no parse (#DL213). A fronteira é
    `spent < cap → ok` (bloqueia em `>=`) nos dois sítios."""
    return env_float("ALPHA_MAX_SESSION_COST_USD")


def _ordered_batch_results(
    tool_calls: list[dict],
    batch_results: list[tuple[str, dict]],
) -> list[dict]:
    """Reordena os `tool_result` de um batch pela ordem das `tool_calls`.

    O executor yielda os resultados agrupados por categoria (validação →
    denials → hook-blocks → sucessos do gather), não em ordem de chamada
    — appendar na ordem de yield quebraria a invariante
    `_recent_calls[i] ↔ _recent_results[i]` de que o resgate
    `_args_distinct` do stale-progress depende (#DL211). Pareia pelo `id`
    estável do evento; ids desconhecidos vão para o fim, em ordem de
    chegada (sort estável)."""
    order = {str(tc.get("id") or ""): i for i, tc in enumerate(tool_calls)}
    return [
        result
        for _, result in sorted(
            batch_results, key=lambda pair: order.get(pair[0], len(order))
        )
    ]


def _track_and_detect_loop(
    call_sigs: list[str],
    recent_calls: list[str],
    recent_results: list[str],
    recent_failures: list[bool],
) -> str | None:
    """Registra o batch corrente nas janelas e roda os 5 detectores.

    Gate on accumulated tool calls, not iteration count: a single iteration
    with a large parallel batch can fill recent_calls with 10+ entries before
    iteration 2, so a per-iteration threshold misses loops that emerge across
    early batch-heavy turns (AUDIT_V1.2 #018). AUDIT_V2.2 #008: this gate
    keys on recent_calls (appended before tool execution), but the
    stale-progress and error-streak detectors read recent_results /
    recent_failures (appended after execution). On a batch-heavy turn the
    gate can open one iteration before those windows fill — harmless, since
    each detector guards its own window size internally and simply catches
    the loop one iteration later."""
    recent_calls.extend(call_sigs)
    if len(recent_calls) > _RECENT_WINDOW_CAP:
        recent_calls[:] = recent_calls[-_RECENT_WINDOW_CAP:]
    if len(recent_calls) < _LOOP_DETECT_MIN_CALLS:
        return None
    return _detect_loop(call_sigs, recent_calls, recent_results, recent_failures)


def _handle_error_streak_nudge(
    final_event: dict,
    messages: list[dict],
    call_sigs: list[str],
    recent_calls: list[str],
    recent_results: list[str],
    recent_failures: list[bool],
    loop_reason: str,
    iteration: int,
) -> dict:
    """Primeira detecção de streak: intervir sem terminar. Preserva o texto
    do assistant, DESCARTA os tool_calls não executados (como
    _produce_forced_response — tool_calls órfãos seriam HTTP 400) e injeta
    um nudge user pedindo mudança de abordagem. O loop segue com tools; se
    um novo streak se formar, cai no caminho terminal.

    Muta `messages` e as janelas por referência; retorna o anomaly event.
    """
    logger.warning(
        "Error streak (%s) at iteration %d — nudging instead of forcing final",
        loop_reason, iteration + 1,
    )
    if final_event.get("content"):
        streak_msg: dict = {"role": "assistant", "content": final_event["content"]}
        if final_event.get("reasoning_content"):
            streak_msg["reasoning_content"] = final_event["reasoning_content"]
        messages.append(streak_msg)
    messages.append({
        "role": "user",
        "content": _ERROR_STREAK_NUDGE.format(loop_reason=loop_reason),
    })
    # Janela de falhas zerada (em lockstep com results — são alinhados 1:1)
    # para o mesmo streak não re-disparar já na próxima iteração antes de o
    # modelo ter chance de corrigir. AUDIT_V2.3 #205: remove também os
    # sigs-FANTASMA do batch descartado (appendados no extend, mas nunca
    # executados) — senão os detectores exato/similar/ciclo contam chamadas
    # que não rodaram e o alinhamento call↔result degrada.
    if call_sigs:
        del recent_calls[-len(call_sigs):]
    recent_results.clear()
    recent_failures.clear()
    return {
        "type": "anomaly",
        "reason": "error_streak_nudge",
        "detail": (
            f"{loop_reason} — pausa para correção de rota "
            "(tools continuam disponíveis)"
        ),
    }


def _handle_preflight_reprompt(
    final_event: dict,
    messages: list[dict],
    get_tool_fn,
    reprompts_this_task: int,
    iteration: int,
) -> int | None:
    """If the turn plans >= PREFLIGHT_REQUIRED_DESTRUCTIVE_COUNT destructive
    tools without first calling `pre_flight`, append a corrective note to
    `messages` and signal a re-prompt. Extracted from run_agent (AUDIT_V2.2
    #020).

    Returns the new re-prompt count when a re-prompt fired (caller resets
    `dsml_failures` and ``continue``s), else None (caller proceeds to execute
    the tools).
    """
    if not (
        reprompts_this_task < PREFLIGHT_REPROMPT_LIMIT
        and get_tool_fn is not None
        and not any(
            tc.get("name") == "pre_flight" for tc in final_event["tool_calls"]
        )
    ):
        return None

    n_destructive = _count_destructive_non_planning(
        final_event["tool_calls"], get_tool_fn
    )
    if n_destructive < PREFLIGHT_REQUIRED_DESTRUCTIVE_COUNT:
        return None

    # Preserve assistant text reasoning but DROP the tool_calls — appending
    # tool_calls without matching tool responses would 400 the next provider
    # request (same pattern as loop detection above).
    if final_event.get("content"):
        messages.append({
            "role": "assistant",
            "content": final_event["content"],
        })
    messages.append({
        "role": "user",
        "content": (
            "[ALPHA SYSTEM NOTE] You planned "
            f"{n_destructive} destructive tool calls "
            "this turn without calling `pre_flight` "
            "first. Per the PLANNING rule in your "
            "system prompt, call `pre_flight(goal, "
            "steps, confidence)` now to show the user "
            "the strategy + cost estimate, then proceed "
            "with the planned tools after approval."
        ),
    })
    logger.info(
        "pre_flight re-prompt fired at iteration %d "
        "(planned %d destructive tools, no pre_flight)",
        iteration + 1, n_destructive,
    )
    return reprompts_this_task + 1


async def run_agent(
    messages: list[dict],
    user_message: str,
    temperature: float = 0.5,
    provider: str | None = None,
    get_tool_fn=None,
    tools: list[dict] | None = None,
    approval_callback=None,
    max_iterations: int | None = None,
    max_turn_seconds: float | None = None,
    workspace: str | None = None,
    bg_notifications: bool = True,
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
        provider: LLM provider name (None resolves to config.DEFAULT_PROVIDER).
        get_tool_fn: Function(name) -> ToolDefinition for looking up tools.
        tools: OpenAI-format tool definitions list.
        approval_callback: Sync function(tool_name, args) -> bool for approval.
        max_iterations: Override iteration limit (defaults to MAX_ITERATIONS).
        max_turn_seconds: Override wall-clock budget per turn (defaults to
            MAX_TURN_SECONDS).
        workspace: Workspace directory injected into tool execution context
            (sub-agent isolation / scratch dirs).
        bg_notifications: When True, finished background tasks are injected
            as a synthetic notification on the next iteration.

    Yields:
        {"type": "token", "text": "..."}
        {"type": "thinking", "text": "..."}   — reasoning delta (display-only)
        {"type": "tool_call", "name": ..., "args": ...}
        {"type": "tool_result", "name": ..., "result": ...}
        {"type": "approval_needed", "name": ..., "args": ...}
        {"type": "context_compressed", "before": int, "after": int}
        {"type": "done", "reply": "full text"}
        {"type": "error", "message": "..."}
    """
    if tools is None:
        tools = []
    if provider is None:
        # #DM071: fonte única do default — mesmo padrão de sdk.run_task.
        provider = DEFAULT_PROVIDER

    iteration_limit = max_iterations if max_iterations is not None else MAX_ITERATIONS
    turn_budget_seconds = (
        max_turn_seconds if max_turn_seconds is not None else MAX_TURN_SECONDS
    )
    full_response = ""
    # Cache the model once — get_provider_config rebuilds the config dict
    # and re-reads env vars on every call; agent loop hits this on every
    # final event. Fall back to "" if config lookup fails (e.g. API key
    # missing in a test) so cost tracking degrades gracefully rather than
    # aborting the whole turn.
    try:
        _provider_model = get_provider_config(provider).get("model", "")
    except Exception:
        _provider_model = ""

    # Track tool calls for smart loop detection
    _recent_calls: list[str] = []
    _recent_results: list[str] = []
    # Per-result failure flags (aligned 1:1 with _recent_results) for the
    # error-streak detector — classified from the structured result dict, not
    # the lossy string preview (#D104).
    _recent_failures: list[bool] = []

    # Pre-flight enforcement counter (slice 2). Per-task, not per-iteration:
    # once we've re-prompted once we accept that the next planning attempt
    # is the agent's final word and execute whatever it returns. Avoids
    # infinite re-prompt loops on providers that resist instruction.
    _preflight_reprompts_this_task = 0

    # Error-streak recovery (um nudge por task). Diferente dos outros 4
    # detectores (repetição exata/similar/ciclo/stale — loops de verdade),
    # um streak de falhas costuma ser trabalho legítimo tocando em coisas
    # que retornam não-zero (grep sem match, pytest indisponível numa
    # auditoria). Matar o turno joga fora o progresso; a 1ª detecção vira
    # um nudge com tools ainda disponíveis, e só a 2ª força a resposta.
    _error_streak_nudged = False

    # Cache the session-cap parse once per task — env var doesn't change
    # mid-run, and parsing it every iteration is wasted work when the
    # env var is set (which is when the parse happens).
    _session_cap_usd = session_cost_cap()

    def _session_cap_breach() -> dict | None:
        """Return an `error` event if the session cost cap is exceeded, else
        None. Shared by the top-of-iteration gate and the post-stream recheck
        (#012) so the message and threshold logic stay in one place."""
        if _session_cap_usd is None:
            return None
        spent = session_cost_usd()
        if spent < _session_cap_usd:
            return None
        return {
            "type": "error",
            "message": (
                f"Session cost ${spent:.4f} reached "
                f"ALPHA_MAX_SESSION_COST_USD=${_session_cap_usd:.4f}. "
                f"Session aborted. Use /cost to inspect or "
                f"raise the cap and restart."
            ),
        }

    # AUDIT_V2.3 #201: o cap por-turno (ALPHA_MAX_TURN_COST_USD) era checado
    # apenas DENTRO da tool `pre_flight` — opcional/discricionária, com
    # estimativa derivada de `args_preview` controlado pelo modelo. Agora o
    # enforcement espelha o cap de sessão, no loop: (a) gate cumulativo sobre
    # o spend REAL do turno (snapshot na entrada do run_agent = início do
    # turno; cada mensagem do usuário cria um run_agent novo) e (b) gate
    # pré-execução que soma a estimativa do batch corrente — calculada dos
    # `arguments` REAIS, não de preview — antes de disparar as tools.
    # `pre_flight` continua exibindo a estimativa no card (UI), mas o
    # enforcement não depende de o modelo chamá-la.
    _turn_cap_usd = env_float("ALPHA_MAX_TURN_COST_USD")
    # Snapshot só quando o cap está ativo — sem cap, nenhum gate lê o valor.
    _turn_start_cost_usd = session_cost_usd() if _turn_cap_usd is not None else 0.0

    def _turn_cap_breach(batch_estimate_usd: float = 0.0) -> dict | None:
        """Error event se o custo real do turno (+ estimativa do batch
        prestes a executar) atingir o cap por-turno; senão None."""
        if _turn_cap_usd is None:
            return None
        spent = session_cost_usd() - _turn_start_cost_usd
        if spent + batch_estimate_usd < _turn_cap_usd:
            return None
        detail = f"Custo real do turno ${spent:.4f}"
        if batch_estimate_usd:
            detail += f" + batch estimado ${batch_estimate_usd:.4f}"
        return {
            "type": "error",
            "message": (
                f"{detail} atinge ALPHA_MAX_TURN_COST_USD="
                f"${_turn_cap_usd:.4f}. Turno encerrado"
                + (" antes de executar o batch" if batch_estimate_usd else "")
                + ". Divida a tarefa, aumente o cap ou remova passos."
            ),
        }

    # Counter for DSML drift attempts (DeepSeek-style <invoke> markup
    # leaking into content). First failure → corrective re-prompt;
    # second consecutive → abort with anomaly. Reset on any successful
    # tool_calls emission.
    dsml_failures = 0

    # Wall-clock budget for the whole turn (LIMITS["max_turn_seconds"]).
    # When the agent has been working for longer than this, the next
    # iteration emits a hard_cap anomaly and stops calling tools — the
    # user gets a clear "turno encerrado por tempo" instead of a 10+
    # minute "still going" spinner.
    _turn_start = time.monotonic()
    # Time blocked on approval prompts is discounted (see _turn_budget).
    from .._turn_budget import user_wait_total as _user_wait_total
    _wait_base = _user_wait_total()

    # CODE_REVIEW_FASE1 #14 + CODE_REVIEW2 #1/#4: propagate the turn
    # deadline into the executor via ContextVar so individual tool
    # calls can't run past the turn budget. `use_deadline` is a
    # context manager with try/finally — when sub-agents are awaited
    # sequentially in the parent's asyncio task (delegate_task path),
    # their deadline does NOT leak back to the parent. `cap_to_inherited`
    # ensures a sub-agent's budget never extends past the parent's.
    # Deadline propagation moved to callers (main.py:_run_once and
    # delegate_tools._run_subagent) which wrap `async for ... in
    # run_agent(...)` with `use_deadline(...)` context manager. That
    # avoids re-indenting the entire iteration loop here and lets the
    # caller scope the ContextVar lifetime via try/finally — fixing
    # CODE_REVIEW2 #1 (sequential delegate_task leak) and #4 (sub-agent
    # budget cap by inherited).

    for iteration in range(iteration_limit):
        logger.info("Agent iteration %d/%d", iteration + 1, iteration_limit)
        record_iteration()

        # ── Background task notifications (#503: helper) ──
        # Sub-agentes passam bg_notifications=False: a task pertence à raiz.
        if bg_notifications:
            _bg_anom = _drain_bg_notifications(messages)
            if _bg_anom is not None:
                yield _bg_anom
        # Parent → background child inbox. Gated by the child's env
        # (ALPHA_BG_INBOX), NOT by bg_notifications: print mode passes
        # bg_notifications=False and the child IS a print-mode process.
        _inbox_anom = _drain_bg_inbox(messages)
        if _inbox_anom is not None:
            yield _inbox_anom

        # ── Wall-clock budget per turn (#503: helper) ──
        # The user sees a clear anomaly and gets the prompt back; if there's
        # more work left the LLM can signal it with [CONTINUE: ...].
        _elapsed = _check_wall_clock_cap(
            _turn_start + (_user_wait_total() - _wait_base),
            turn_budget_seconds, iteration,
        )
        if _elapsed is not None:
            # Wall-clock safety net hit. The model never volunteered a final
            # answer, so force one no-tools summary (fresh LLM call, not
            # clamped to the now-exhausted turn budget) — the user always
            # gets a response synthesizing the work done so far.
            async for event in _force_final_summary(
                messages, temperature, provider, _provider_model,
                full_response, REASON_MAX_TURN_SECONDS,
                f"orçamento de tempo do turno excedido ({_elapsed:.0f}s "
                f">= {int(turn_budget_seconds)}s)",
            ):
                yield event
            return

        # ── Budget caps (Plano-v3 pre-flight slice 2 + AUDIT_V2.3 #201) ──
        # Session cap: a long-running agent can't quietly accumulate cost
        # across many turns. Turn cap: spend real acumulado DESTE turno
        # (gate cumulativo — o pré-execução por batch fica adiante).
        # Unset = no cap.
        _cap_breach = _session_cap_breach() or _turn_cap_breach()
        if _cap_breach is not None:
            yield _cap_breach
            return

        # ── Pre-call adaptive compression ──
        # CODE_REVIEW3 #1 + CODE_REVIEW4 #1: clamp via shared helper
        # (was previously 25 lines duplicated across 3 sites).
        _to = _llm_budget_timeout()
        try:
            async with asyncio.timeout(_to):
                async for evt in _maybe_compress(messages, provider):
                    yield evt
        except TimeoutError:
            _anom, _done, _log = _llm_budget_timeout_events(
                REASON_COMPRESSION_TIMEOUT_BY_BUDGET, "Context compression",
                full_response, _to,
            )
            logger.warning(_log)
            yield _anom
            yield _done
            return

        # ── Stream LLM call (with one in-band overflow retry) ──
        # CODE_REVIEW2 #3 + CODE_REVIEW4 #1: clamp via shared helper.
        _to = _llm_budget_timeout()
        final_event = None
        # Lazy tools: recomputed EVERY iteration so schemas pulled in by a
        # `tool_search` call in this same turn are present on the next LLM
        # call. Only narrows/annotates `tools`; never widens it.
        from ..tool_lazy import apply as _lazy_apply
        _iter_tools = _lazy_apply(tools, get_tool_fn)
        try:
            async with asyncio.timeout(_to):
                async for event in _stream_one_turn(
                    messages, _iter_tools, temperature, provider, _provider_model
                ):
                    if event["type"] == "_final":
                        final_event = event["event"]
                    else:
                        yield event
        except TimeoutError:
            _anom, _done, _log = _llm_budget_timeout_events(
                REASON_LLM_TIMEOUT_BY_BUDGET, "LLM stream", full_response, _to,
            )
            logger.warning(_log)
            yield _anom
            yield _done
            return

        if final_event is None:
            yield {"type": "error", "message": "No response from LLM"}
            return

        # DSML drift handling MUST come before the generic error gate.
        # CODE_REVIEW_FASE1 #2: when the provider emits a diagnostic
        # finish_reason (e.g. Gemini MALFORMED_FUNCTION_CALL) AND the
        # content carried <invoke> markup, llm.py sets BOTH `error` and
        # `dsml_recovery_failed=True`. Returning on `error` first would
        # bypass the corrective re-prompt — exactly the case the patch
        # was designed to fix. Check the drift flag first; if the
        # corrective branch can't fire (2-strike abort), it falls
        # through to the error path below via the explicit yield+return.
        _dsml_action, dsml_failures, _dsml_events = _handle_dsml_drift(
            final_event, messages, dsml_failures, full_response
        )
        for _ev in _dsml_events:
            yield _ev
        if _dsml_action == "abort":
            return
        if _dsml_action == "reprompt":
            logger.info(
                "DSML drift detected (attempt %d/2) — re-prompted with correction",
                dsml_failures,
            )
            continue  # loop back to next iteration with corrective context

        # LLM error (non-overflow, or overflow that survived the retry).
        # Checked AFTER dsml_recovery so the Gemini MALFORMED + drift
        # case can recover (CODE_REVIEW_FASE1 #2).
        if final_event.get("error"):
            yield {"type": "error", "message": final_event["error"]}
            return

        # Accumulate text
        if final_event.get("content"):
            full_response += final_event["content"]

        # No tool calls = the model stopped this turn.
        if not final_event.get("tool_calls"):
            # Background child: a parent message that arrived while the model
            # was finishing must not be lost — inject it and keep going (the
            # iteration/wall-clock caps still bound this).
            _inbox_anom = _drain_bg_inbox(messages, final_event.get("content"))
            if _inbox_anom is not None:
                yield _inbox_anom
                continue
            # If it gave a real final answer (content THIS turn) that's the
            # conclusion → done. But if it went SILENT (no content) right after
            # doing tool work, many models (e.g. DeepSeek) just stop without the
            # `※ recap`/summary the system prompt asks for — the turn looks
            # unfinished even though the work happened. Force ONE summary so the
            # user always gets a conclusion. Bounded: single LLM call, then done
            # — `_recent_calls` non-empty means tools ran, so this never fires
            # for plain chat/question turns.
            stopped_silent = not (final_event.get("content") or "").strip()
            if stopped_silent and _recent_calls:
                logger.info(
                    "Model stopped silently after tool work — forcing a final summary"
                )
                async for event in _stream_forced_summary(
                    messages, temperature, provider, _provider_model, full_response,
                    "[ALPHA SYSTEM NOTE] You stopped without a final message after "
                    "doing work this turn. " + _FORCED_SUMMARY_INSTRUCTION,
                    "silent_stop",
                    "modelo parou sem resposta final após trabalho — resumo forçado",
                    "Final summary",
                ):
                    yield event
                return
            yield {"type": "done", "reply": full_response}
            return

        # Successful tool_calls — reset DSML failure counter
        dsml_failures = 0

        # #012: re-check the caps AFTER the streaming call that just
        # recorded its usage, BEFORE executing this turn's tools. The
        # top-of-iteration gate only catches the overshoot on the NEXT turn —
        # by then this turn's tools have already run, and those may include
        # delegate_* sub-agents that spend MORE. Re-checking here bounds the
        # overshoot to the single streaming call that breached the cap.
        _cap_breach = _session_cap_breach() or _turn_cap_breach()
        if _cap_breach is not None:
            yield _cap_breach
            return

        # ── Smart loop detection (#503: helpers) ──
        call_sigs = [_call_signature(tc) for tc in final_event["tool_calls"]]
        loop_reason = _track_and_detect_loop(
            call_sigs, _recent_calls, _recent_results, _recent_failures
        )

        if loop_reason and loop_reason.startswith("error streak") and not _error_streak_nudged:
            _error_streak_nudged = True
            yield _handle_error_streak_nudge(
                final_event, messages, call_sigs,
                _recent_calls, _recent_results, _recent_failures,
                loop_reason, iteration,
            )
            continue

        if loop_reason:
            logger.warning(
                "Loop detected (%s) at iteration %d — forcing final response",
                loop_reason, iteration + 1,
            )
            # CODE_REVIEW3 #2 + CODE_REVIEW4 #1: clamp via shared helper.
            _to = _llm_budget_timeout()
            try:
                async with asyncio.timeout(_to):
                    async for event in _produce_forced_response(
                        messages, temperature, provider, _provider_model,
                        loop_reason, final_event, full_response,
                    ):
                        yield event
            except TimeoutError:
                _anom, _done, _log = _llm_budget_timeout_events(
                    REASON_FORCED_RESPONSE_TIMEOUT_BY_BUDGET,
                    "Forced response (loop recovery)",
                    full_response, _to,
                )
                logger.warning(_log)
                yield _anom
                yield _done
            return

        # ── Pre-flight enforcement (Plano-v3 RFC slice 2) ──
        # When the agent plans 2+ destructive tools in a turn without
        # calling pre_flight first, inject a synthetic user note asking
        # it to re-plan with pre_flight. Once per task — repeated misses
        # fall through to execute as-is (provider won't comply, no point
        # looping).
        _pf_count = _handle_preflight_reprompt(
            final_event, messages, get_tool_fn,
            _preflight_reprompts_this_task, iteration,
        )
        if _pf_count is not None:
            _preflight_reprompts_this_task = _pf_count
            # CODE_REVIEW_FASE1 #12: also reset dsml_failures here so the
            # "consecutive failures" semantics actually means consecutive —
            # pre_flight reprompt counts as productive iteration progress,
            # not a missed correction.
            dsml_failures = 0
            continue  # restart the iteration

        # ── AUDIT_V2.3 #201(b): gate pré-execução do cap por-turno ──
        # Estima o custo do batch a partir dos `arguments` REAIS (não do
        # `args_preview` que o modelo escolhe mostrar no pre_flight) e
        # bloqueia ANTES de executar — e antes do append do assistant
        # message, para não deixar tool_calls órfãos no histórico.
        if _turn_cap_usd is not None:
            _batch_steps = [
                {
                    "tool": str(tc.get("name", "")),
                    "args_preview": str(tc.get("arguments", "")),
                }
                for tc in final_event["tool_calls"]
            ]
            _cap_breach = _turn_cap_breach(
                estimate_total_cost(_batch_steps, _provider_model)
            )
            if _cap_breach is not None:
                yield _cap_breach
                return

        # Process tool calls
        messages.append(
            build_assistant_tool_message(
                final_event["content"],
                final_event["tool_calls"],
                final_event.get("reasoning_content"),
                final_event.get("thinking_blocks"),
            )
        )

        _batch_results: list[tuple[str, dict]] = []
        try:
            async for event in execute_tool_calls(
                final_event["tool_calls"],
                messages,
                needs_approval_fn=needs_approval,
                is_denied_fn=is_denied,
                approval_callback=approval_callback,
                get_tool_fn=get_tool_fn,
                workspace=workspace,
                provider=provider,
            ):
                yield event
                # Track tool results for stale progress detection —
                # bufferizados por id e appendados em ordem de chamada
                # após o batch (_ordered_batch_results).
                if event.get("type") == "tool_result":
                    _batch_results.append(
                        (str(event.get("id") or ""), event.get("result", {}))
                    )
        except asyncio.CancelledError:
            # AUDIT_V1.8 #045: backfill movido de finally para except —
            # no happy-path roda O(N) scan por iteracao (1.25M touches
            # em task de 50 turns). So precisa rodar quando houve
            # interrupcao real (CancelledError ou Exception).
            _backfill_interrupted_tool_responses(messages)
            raise
        except Exception as e:
            _backfill_interrupted_tool_responses(messages)
            logger.exception("Tool execution failed: %s", e)
            yield {"type": "error", "message": f"Tool execution failed: {e}"}
            return

        for result in _ordered_batch_results(final_event["tool_calls"], _batch_results):
            _recent_results.append(_result_preview(result, 500))
            _recent_failures.append(_result_is_failure(result))
        # Truncar para evitar leak de memoria em sessoes longas;
        # `_recent_calls` ja faz isso, `_recent_results` nao fazia.
        # _recent_failures e truncado em lockstep para manter o
        # alinhamento 1:1 com _recent_results.
        if len(_recent_results) > _RECENT_WINDOW_CAP:
            _recent_results[:] = _recent_results[-_RECENT_WINDOW_CAP:]
            _recent_failures[:] = _recent_failures[-_RECENT_WINDOW_CAP:]

        # Re-check dos caps logo após um batch com delegate_* — sub-agentes
        # gastam DURANTE a execução das tools, e o gate do topo da próxima
        # iteração não roda quando esta era a última (o loop cairia direto
        # no resumo forçado, gastando ainda mais com o cap já estourado).
        if any(
            str(tc.get("name", "")).startswith("delegate_")
            for tc in final_event["tool_calls"]
        ):
            _cap_breach = _session_cap_breach() or _turn_cap_breach()
            if _cap_breach is not None:
                yield _cap_breach
                return

    # Max iterations reached. The model kept calling tools and never
    # volunteered a final answer, so force one no-tools summary — the user
    # always gets a response synthesizing the work done, instead of the empty
    # `done` (old behavior) or a silent auto-restart (V1.8, now removed).
    async for event in _force_final_summary(
        messages, temperature, provider, _provider_model,
        full_response, REASON_MAX_ITERATIONS,
        f"limite de {iteration_limit} iterações atingido",
    ):
        yield event
