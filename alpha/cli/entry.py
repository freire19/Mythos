#!/usr/bin/env python3
"""
Alpha Code — Standalone terminal agent (CLI entry point).

AUDIT_V2.3 #702: este módulo morava em `main.py` na raiz do repo e era
embarcado no wheel como módulo top-level `main` (nome genérico — colisão
de namespace em venvs compartilhados). Agora vive dentro do pacote; o
`main.py` da raiz é só um shim de desenvolvimento.

Usage:
    alpha                                    # interactive REPL (instalado)
    python -m alpha                          # idem
    python main.py "analyze this project"    # checkout de desenvolvimento
    alpha --provider grok "fix the bug"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
# #DM067: import único no topo — antes `_run_once` importava `time` DUAS
# vezes sob dois aliases (`_time` para .time(), `_t` para .monotonic()).
import time

# Auto-continue marker `[CONTINUE: <next step>]` — implementação canônica
# em alpha.sdk (reusada pelo modo print e por embedders); o alias preserva
# os call sites do REPL.
from alpha.sdk import extract_continue as _extract_continue  # noqa: E402

_AUTO_CONTINUE_MAX_DEFAULT = 10

# init_console DEVE rodar antes de `from alpha.display` — senao os
# primeiros ANSI escapes saem pro terminal Windows sem traducao.
from alpha._platform import init_console
init_console()

from alpha.agents import get_agent
from alpha.attachments import build_user_content
from alpha.ocr import ocr_images_sync
from alpha.config import (
    DEFAULT_PROVIDER,
    get_available_providers,
    get_provider_config,
    show_thinking_mode,
)
from alpha import hooks
from alpha.mcp import list_active_servers as list_mcp_servers
from alpha.skills import list_skills
from alpha.display import (
    C,
    ThinkingIndicator,
    c,
    format_assistant_text,
    format_context_indicator,
    format_user_echo,
    is_auto_accept,
    print_banner,
    print_context_compressed,
    print_context_warning,
    print_error,
    print_phase,
    print_providers_list,
    print_thinking,
    print_tool_call,
    print_tool_result,
    print_turn_summary,
    render_markdown,
)
from alpha.display.core import live_label_for_tool
from alpha.display.thinking import erase_active_indicator_line
from alpha.tools.skill_tools import clear_active_skill
from alpha import plan_mode
from alpha.history import (
    generate_session_id,
    get_last_session_id,
    load_session,
    save_session,
)
from alpha.cli.commands import DispatchResult, ReplContext, dispatch
from alpha.cli.lifecycle import install_lifecycle_hooks
from alpha.cli.setup import (
    approval_callback as _approval_callback,
    build_system_prompt as _build_system_prompt,
    get_tools_for_agent as _get_tools_for_agent,
    pick_provider_interactive as _pick_provider_interactive,
    resolve_active_agent as _resolve_active_agent,
)

install_lifecycle_hooks()

# Opt-in JSON-lines logs to ~/.alpha/logs/alpha-YYYYMMDD.log when
# ALPHA_JSON_LOGS=1 is set. No-op otherwise.
from alpha.jsonlogs import maybe_install as _maybe_install_jsonlogs  # noqa: E402
_maybe_install_jsonlogs()


# ─── run_repl helpers ───


def _print_startup_status(provider: str, cfg: dict, tools: list, active_agent) -> None:
    """Print the post-banner status lines (tools, skills, MCP, project ctx)."""
    if not cfg["supports_tools"]:
        print_phase(f"{c(C.YELLOW, 'chat-only')} — {provider} does not support tool-calling")
    elif tools:
        print_phase(f"Loaded {len(tools)} tools")
    else:
        print_phase("No tools loaded — running in chat-only mode")

    skills_count = len(list_skills())
    if skills_count:
        print_phase(f"Loaded {skills_count} skills")

    mcp_servers = list_mcp_servers()
    if mcp_servers:
        total_mcp_tools = sum(len(s["tools"]) for s in mcp_servers)
        print_phase(
            f"MCP: {len(mcp_servers)} server(s), {total_mcp_tools} tool(s)"
        )

    if active_agent:
        print_phase(f"Active agent: {active_agent.name}")

    # Surface auto-loaded project context so the user sees what Alpha
    # picked up. Uses the same loader as `_build_system_prompt` — both
    # paths are cheap (single read, capped at MAX_BYTES) so the redundant
    # call is fine.
    from alpha.project_context import load_project_context
    _proj_ctx = load_project_context()
    if _proj_ctx is not None:
        rel = os.path.relpath(_proj_ctx.path)
        size_kb = _proj_ctx.raw_size / 1024
        suffix = " (truncated)" if _proj_ctx.truncated else ""
        print_phase(f"Project context: {rel} ({size_kb:.1f} KB){suffix}")


def _try_at_dispatch(
    user_input: str, default_provider: str, default_temperature: float
) -> bool:
    """`@name message` — fire a one-shot agent without leaving the REPL.

    Returns True if the input matched and was handled (caller should
    `continue` the REPL loop), False otherwise.
    """
    if not user_input.startswith("@"):
        return False
    parts = user_input.split(maxsplit=1)
    if len(parts) != 2 or len(parts[0]) <= 1:
        return False

    at_name = parts[0][1:]
    at_msg = parts[1]
    at_agent = get_agent(at_name)
    if at_agent is None:
        print_error(f"Agent not found: {at_name}")
        return True

    at_provider = at_agent.provider or default_provider
    at_temperature = (
        at_agent.temperature if at_agent.temperature is not None
        else default_temperature
    )
    at_system = _build_system_prompt(at_agent)
    at_get_tool, at_tools = _get_tools_for_agent(at_agent)

    print(c(C.GRAY, f"  (one-shot → {at_name})"))
    print()

    cwd = os.getcwd()
    at_messages = [
        {"role": "system", "content": at_system},
        {"role": "user", "content": f"[CWD: {cwd}]\n{at_msg}"},
    ]
    try:
        asyncio.run(
            _run_once(
                at_messages, at_msg, at_provider, at_temperature,
                at_get_tool, at_tools,
                workspace=at_agent.workspace,
            )
        )
    except KeyboardInterrupt:
        print(c(C.YELLOW, "\n\nInterrupted."))
    print()
    return True


def _apply_plan_mode_note(text: str) -> str:
    """Plan mode (camada 3): prefixa a instrução read-only ao turno.

    AUDIT_V2.3 #602: extraído para helper puro justamente para ser testável
    — deletar o prepend quebra `tests/test_plan_mode.py::TestLayer3Note`.
    """
    if plan_mode.is_active():
        return f"{plan_mode.PLAN_MODE_NOTE}\n\n{text}"
    return text


def _attach_ocr_if_needed(
    contextualized: str,
    image_paths: list,
    cfg: dict,
    provider: str,
) -> tuple[str, list]:
    """Run Gemini OCR over images when the active provider isn't vision-capable.

    Returns (possibly-augmented contextualized text, possibly-emptied
    image_paths). When the provider DOES support vision, the call is a
    no-op pass-through.
    """
    if not image_paths or cfg.get("supports_vision", False):
        return contextualized, image_paths

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if gemini_key:
        sys.stdout.write(f"  {c(C.GRAY, '✾ Analisando imagem...')}")
        sys.stdout.flush()
        ocr_text = ocr_images_sync(image_paths, gemini_key)
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        if ocr_text:
            contextualized = (
                f"{contextualized}\n\n"
                f"[Conteudo extraido da imagem via OCR (Gemini)]:\n{ocr_text}"
            )
        else:
            print_error("  OCR falhou — imagem ignorada.")
    else:
        print_error(
            f"Provider '{provider}' (modelo {cfg['model']}) nao suporta imagens. "
            f"Imagem(s) ignorada(s). Defina GEMINI_API_KEY para OCR automatico "
            f"ou use /provider para um modelo vision-capable (google, openai, anthropic)."
        )
    return contextualized, []


# CODE_REVIEW_FASE1 #8: derive the write-tool set + path-arg names
# from the canonical PATH_PARAMS_BY_TOOL registry instead of an
# ad-hoc map. The old hand-rolled map listed keys like "file_path",
# "filename", "target" that no real tool schema actually uses, so
# `files_touched` was silently empty in most turns.
_WRITE_TOOLS = {"write_file", "edit_file", "search_and_replace"}


class _TurnState:
    """Acumuladores + rendering de um turno do agente (#DM067).

    Extraído de `_run_once`: um método-handler por tipo de evento (ver
    `_EVENT_HANDLERS`) no lugar do dispatch inline de 8 ramos. Todos os
    colaboradores de display (`ThinkingIndicator`, `print_tool_call`,
    `render_markdown`, …) são resolvidos como globals do módulo em tempo
    de chamada — monkeypatch em `entry.<nome>` continua funcionando.
    """

    def __init__(self) -> None:
        # AUDIT_V2.2 #019: accumulate streamed tokens in lists + join,
        # instead of `str += token` per token (which copies the growing
        # string each time). Mirrors the _StreamAccumulator discipline
        # already used in llm.py.
        self.full_reply_parts: list[str] = []
        self.pending_parts: list[str] = []
        # Reasoning streamed since the last flush (extended thinking /
        # reasoning_content). Rendered dim by `flush_text`, BEFORE the
        # reply segment it preceded; never part of the reply itself.
        self.thinking_parts: list[str] = []
        self.thinking_mode = show_thinking_mode()
        self.indicator = ThinkingIndicator("Think")
        self.pending_args: dict[str, dict] = {}
        # Header bookkeeping for parallel batches. All `tool_call` events of
        # a batch arrive before the first result; printing every header up
        # front left the results stacked underneath with no way to tell
        # which belonged to which call. Only the first header of a batch is
        # printed right away — the others are printed together with their
        # result. `header_on_screen` is the call whose header is the last
        # thing printed (its result can go straight under it).
        self.call_info: dict[str, tuple[str, dict, str]] = {}
        self.header_on_screen: str | None = None
        # End-of-turn telemetry — used by print_turn_summary() in finally.
        self.start = time.time()
        self.tools_count = 0
        self.errors_count = 0
        self.files_touched: set[str] = set()
        self.anomaly: dict | None = None

    def _record_touched(self, name: str, args: dict | None) -> None:
        from alpha.agents.workspace import PATH_PARAMS_BY_TOOL
        if not isinstance(args, dict) or name not in _WRITE_TOOLS:
            return
        for key in PATH_PARAMS_BY_TOOL.get(name, ()):
            val = args.get(key)
            if isinstance(val, str) and val:
                self.files_touched.add(val)
                break

    def flush_thinking(self) -> None:
        if not self.thinking_parts:
            return
        thought = "".join(self.thinking_parts)
        self.thinking_parts.clear()
        if self.thinking_mode == "off":
            return
        erase_active_indicator_line()
        print_thinking(thought, self.thinking_mode)

    def flush_text(self) -> None:
        self.flush_thinking()
        if not self.pending_parts:
            return
        rendered = format_assistant_text(
            render_markdown("".join(self.pending_parts))
        )
        erase_active_indicator_line()  # clear spinner line before output (non-scroll mode)
        if rendered:
            sys.stdout.write(rendered)
            sys.stdout.flush()
            self.header_on_screen = None
        self.pending_parts.clear()

    # ── event handlers (um por tipo; assinatura uniforme (self, event)) ──

    def on_token(self, event: dict) -> None:
        text = event.get("text", "")
        self.pending_parts.append(text)
        self.full_reply_parts.append(text)
        self.indicator.add_streamed_text(text)

    def on_thinking(self, event: dict) -> None:
        text = event.get("text", "")
        if not text:
            return
        if not self.thinking_parts:
            # First reasoning chunk of this segment: relabel the spinner so
            # the user sees the model is reasoning, not stalled.
            self.indicator.start("Pensando")
        self.thinking_parts.append(text)
        self.indicator.add_streamed_text(text)

    def on_tool_call(self, event: dict) -> None:
        self.flush_text()
        tc_args = event.get("args", {}) or {}
        key = event.get("id") or event["name"]
        safety = event.get("safety", "safe")
        self.call_info[key] = (event["name"], tc_args, safety)
        if not self.pending_args:
            erase_active_indicator_line()
            print_tool_call(event["name"], tc_args, safety)
            self.header_on_screen = key
        # #DB05: pareia args→result por id (fallback: nome) para batches
        # paralelos do mesmo tool não trocarem/perderem args no display.
        self.pending_args[key] = tc_args if isinstance(tc_args, dict) else {}
        self.indicator.start(live_label_for_tool(event["name"], tc_args))
        # CODE_REVIEW_FASE1 #13: defer tools_count / files_touched
        # updates until tool_result confirms execution succeeded.
        # A denied or failed call shouldn't inflate the summary.

    def on_tool_result(self, event: dict) -> None:
        key = event.get("id") or event["name"]
        tr_args = self.pending_args.pop(key, None)
        result = event.get("result", {}) or {}
        erase_active_indicator_line()
        info = self.call_info.pop(key, None)
        if self.header_on_screen != key and info is not None:
            # Deferred (or displaced) header of a parallel batch: print it
            # now so the result below is unambiguous.
            print_tool_call(*info)
        self.header_on_screen = None
        print_tool_result(event["name"], result, args=tr_args)
        if isinstance(result, dict) and result.get("ok") is False:
            self.errors_count += 1
        else:
            # Tool ran and succeeded — now it counts.
            self.tools_count += 1
            self._record_touched(
                event["name"],
                tr_args if isinstance(tr_args, dict) else None,
            )
        self.indicator.start("Think")

    def on_approval_needed(self, event: dict) -> None:
        # Pause only when the callback will actually block on input().
        # With auto-accept on, print_approval_request short-circuits
        # (one-line "auto-approved" print, no input) — pausing here
        # would freeze the spinner through the entire tool execution.
        # The visible silence during a slow tool (delegate_parallel
        # can run 30s+) was the user-reported symptom.
        if not is_auto_accept():
            self.indicator.pause()
            # The approval card lands below whatever header was last
            # printed; unless it is for that very call, the header no
            # longer sits right above its future result.
            asked = event.get("id") or event.get("name")
            if asked != self.header_on_screen:
                self.header_on_screen = None

    def on_context_compressed(self, event: dict) -> None:
        erase_active_indicator_line()
        print_context_compressed(event.get("before", 0), event.get("after", 0))
        self.indicator.start("Think")

    def on_done(self, event: dict) -> None:
        self.indicator.stop()
        reply = event.get("reply", "")
        # Fallback: if nothing was ever streamed, take the `done`
        # payload. Otherwise trust the buffered tokens — earlier
        # segments have already been flushed at tool-call boundaries,
        # so we can only render what's still pending.
        if reply and not self.full_reply_parts:
            self.pending_parts[:] = [reply]
            self.full_reply_parts[:] = [reply]
        self.flush_text()

    def on_error(self, event: dict) -> None:
        self.flush_text()
        self.indicator.stop()
        print_error(event.get("message", "Unknown error"))
        self.errors_count += 1

    def on_anomaly(self, event: dict) -> None:
        if event.get("reason") in ("error_streak_nudge", "bg_task_finished"):
            # Eventos mid-turn informativos, não paradas: mostrar na
            # hora e NÃO guardar como anomalia de fim de turno
            # ("Agente parou:" seria mentira — o turno continua).
            self.flush_text()
            print_phase(f"⚠ {event.get('detail', 'evento em background')}")
        else:
            self.anomaly = {
                "reason": event.get("reason", "unknown"),
                "detail": event.get("detail", ""),
            }


# Tabela de dispatch evento→handler (#DM067). Tipos desconhecidos são
# no-op — mesmo comportamento do if/elif original.
_EVENT_HANDLERS = {
    "token": _TurnState.on_token,
    "thinking": _TurnState.on_thinking,
    "tool_call": _TurnState.on_tool_call,
    "tool_result": _TurnState.on_tool_result,
    "approval_needed": _TurnState.on_approval_needed,
    "context_compressed": _TurnState.on_context_compressed,
    "done": _TurnState.on_done,
    "error": _TurnState.on_error,
    "anomaly": _TurnState.on_anomaly,
}


async def _run_once(messages, user_message, provider, temperature, get_tool_fn, tools, workspace=None):
    """Run a single agent turn and display events.

    Streamed tokens are buffered, not echoed live — the spinner shows
    progress while text accumulates. At each boundary (tool call, done,
    error) the pending segment passes through `render_markdown` so
    tables, bold, code, and headers reach the terminal as ANSI instead
    of raw markdown."""
    from alpha.agent import run_agent

    st = _TurnState()
    st.indicator.start()
    from alpha import notify as _notify
    _notify.turn_started()
    # Remote control (alpha/remote.py): None unless the server is on.
    from alpha import remote as _remote
    _mirror = _remote.turn_mirror()

    aborted = False
    # CODE_REVIEW2 #1 + CODE_REVIEW3 #4: pin the turn deadline via
    # set_deadline + Token reset in finally. Earlier revision used a
    # `with use_deadline(...)` block at 6-space indent that was both
    # non-standard (between try=4 and async-for=8) and exposed to
    # auto-formatter churn. set_deadline + try/finally keeps the body
    # at the original indentation while still guaranteeing reset on
    # any exit path (normal, exception, KeyboardInterrupt).
    from alpha._turn_budget import set_deadline as _set_deadline, reset as _reset_deadline
    from alpha.config import MAX_TURN_SECONDS as _MAX_TURN_SECONDS
    # CODE_REVIEW4 #2: capture the Token INSIDE the try so an exception
    # between the set and the try (improbable today but fragile to
    # future setup additions) still pairs with the finally reset.
    _td_token = None
    # Fronteira do /rewind: marca o início do turno ANTES de qualquer tool
    # rodar, para "desfazer o último turno" ter um ponto de corte preciso.
    from alpha import checkpoints as _checkpoints
    _checkpoints.mark_turn(user_message)
    try:
        _td_token = _set_deadline(time.monotonic() + _MAX_TURN_SECONDS)
        async for event in run_agent(
            messages,
            user_message,
            temperature=temperature,
            provider=provider,
            get_tool_fn=get_tool_fn,
            tools=tools,
            approval_callback=_approval_callback,
            workspace=workspace,
        ):
            handler = _EVENT_HANDLERS.get(event.get("type", ""))
            if handler is not None:
                handler(st, event)
            if _mirror is not None:
                _mirror.feed(event)
    except (KeyboardInterrupt, asyncio.CancelledError):
        aborted = True
        raise
    finally:
        # CODE_REVIEW3 #4 + CODE_REVIEW4 #3: reset the turn-deadline
        # ContextVar Token — paired with _set_deadline above. Narrow
        # except: only LookupError/ValueError are documented for
        # `ContextVar.reset()` (token from another Context, or var has
        # no value at reset time). Anything else is a real bug and
        # should propagate. `_td_token is not None` guards the case
        # where the set never ran (exception between try entry and
        # _set_deadline).
        if _td_token is not None:
            try:
                _reset_deadline(_td_token)
            except (LookupError, ValueError):
                pass  # Token mismatch — rare, safe to swallow.
        st.indicator.stop()
        st.flush_text()
        if _mirror is not None:
            _mirror.finish(
                aborted=aborted, tools=st.tools_count,
                duration=time.time() - st.start,
            )
        # Print summary unconditionally — interrupts (Ctrl+C, cancellation)
        # get a synthetic anomaly tag so the user sees what executed
        # before the cancellation tore the turn down. `finally` runs
        # before the re-raise propagates, so this happens whether the
        # turn completed normally or was aborted.
        if aborted and st.anomaly is None:
            st.anomaly = {
                "reason": "interrupted",
                "detail": "turno interrompido (Ctrl+C ou cancelamento)",
            }
        print_turn_summary(
            tools_count=st.tools_count,
            duration_s=time.time() - st.start,
            files_touched=st.files_touched,
            errors_count=st.errors_count,
            anomaly=st.anomaly,
        )

    # Return the anomaly alongside the reply so the REPL can auto-continue a
    # turn that was cut by a resource cap (budget/iteration) — the model never
    # got to emit a `[CONTINUE: …]` marker in that path, so without this the
    # user has to type "continue" manually.
    return "".join(st.full_reply_parts), st.anomaly


def _apply_agent_overrides(active_agent, provider: str, temperature: float):
    """Apply active-agent overrides to provider and temperature. (#052)"""
    if not active_agent:
        return provider, temperature
    if active_agent.provider:
        provider = active_agent.provider
    if active_agent.temperature is not None:
        temperature = active_agent.temperature
    return provider, temperature


def _setup_agent_session(provider: str, temperature: float):
    """Resolve agent profile and initialize common session components.

    Extracted from run_repl / run_single (#DM056 / AUDIT_V1.10 #051).
    Returns (active_agent, provider, temperature, system_prompt, get_tool_fn, tools).
    """
    active_agent = _resolve_active_agent()
    provider, temperature = _apply_agent_overrides(active_agent, provider, temperature)
    system_prompt = _build_system_prompt(active_agent)
    get_tool_fn, tools = _get_tools_for_agent(active_agent)
    # session_start dispara aqui porque os três modos (REPL/single/print)
    # passam por este setup exatamente uma vez.
    try:
        hooks.fire(
            "session_start",
            workspace=active_agent.workspace if active_agent else None,
            extra={"provider": provider},
        )
    except Exception:
        pass  # hook nunca derruba o boot
    return active_agent, provider, temperature, system_prompt, get_tool_fn, tools


def _render_prompt(messages: list, provider: str, announced_thresholds: set[int]) -> str:
    """Avisos de threshold de contexto + coloração do prompt do REPL (#DM067).

    Muta `announced_thresholds` in place (cada faixa avisa uma vez só).
    """
    from alpha.context import estimate_messages_tokens, get_context_limit
    _used = estimate_messages_tokens(messages)
    _limit = get_context_limit(provider)
    _pct = int((_used / _limit * 100)) if _limit else 0
    for threshold in (50, 70, 90):
        if _pct >= threshold and threshold not in announced_thresholds:
            print_context_warning(_pct, _used, _limit)
            announced_thresholds.add(threshold)
            break
    # Auto-accept mode tints the prompt amber; plan mode, cyan —
    # replaces the bottom-toolbar chip that previously carried this.
    if plan_mode.is_active():
        arrow_color = C.CYAN + C.BOLD
    elif is_auto_accept():
        arrow_color = C.AMBER_SOFT + C.BOLD
    else:
        arrow_color = C.VIOLET + C.BOLD
    ctx_chip = format_context_indicator(messages, provider)
    return f"{ctx_chip}{c(arrow_color, '❯')} "


def _handle_auto_continue(
    next_step: str,
    auto_continues: int,
    auto_continue_max: int,
    queued_inputs: list[str],
) -> int:
    """Enfileira o `[CONTINUE:]` do modelo respeitando o cap da sessão.

    Extraído de run_repl (#DM067). Retorna o contador atualizado; o cap
    (#DL218) imprime o aviso e NÃO enfileira.
    """
    if auto_continues < auto_continue_max:
        auto_continues += 1
        # CODE_REVIEW_FASE1 #5: append (not insert at 0) so
        # input the user typed during the turn — already in
        # queued_inputs — runs FIRST. Their explicit redirect
        # always wins over the model's auto-continuation.
        queued_inputs.append(next_step)
        print(
            f"  {c(C.VIOLET + C.BOLD, '▶')} "
            f"{c(C.GRAY, f'continuando ({auto_continues}/{auto_continue_max}): ')}"
            f"{c(C.GRAY, next_step)}"
        )
    else:
        print(
            f"  {c(C.YELLOW + C.BOLD, '⚠')} "
            f"{c(C.YELLOW, f'limite de auto-continues atingido ({auto_continue_max}) — pause para revisar antes de continuar')}"
        )
    return auto_continues


def run_repl(
    provider: str,
    temperature: float,
    resume: str | None = None,
    remote: str | None = None,
):
    """Interactive REPL loop."""
    # AUDIT_V1.8 #043: prompt_toolkit so carrega em modo REPL —
    # single-shot CLI nao paga o cold-load de ~99ms.
    from alpha.repl_input import read_input
    from alpha import repl_concurrent
    active_agent, provider, temperature, system_prompt, get_tool_fn, tools = \
        _setup_agent_session(provider, temperature)
    cfg = get_provider_config(provider)
    if active_agent and active_agent.model:
        cfg["model"] = active_agent.model

    print_banner(provider, cfg["model"])

    messages = [{"role": "system", "content": system_prompt}]
    history = []
    session_id = generate_session_id()
    announced_thresholds: set[int] = set()

    _print_startup_status(provider, cfg, tools, active_agent)

    if remote:
        from alpha.cli.commands.remote_cmd import start_remote
        start_remote(model=cfg.get("model"), local=(remote == "local"))

    # Messages typed during agent execution land here; we drain before
    # falling back to the blocking read_input() prompt.
    queued_inputs: list[str] = []

    # --continue/--resume: reusa os handlers /continue e /load via fila —
    # o primeiro ciclo do loop despacha como se o usuário tivesse digitado.
    if resume:
        queued_inputs.append(
            "/continue" if resume == "__last__" else f"/load {resume}"
        )

    # Auto-continue counter for the session — bounded so a runaway
    # `[CONTINUE: ...]` chain stops gracefully instead of looping forever.
    auto_continues = 0
    _auto_continue_max = _auto_continue_limit()

    while True:
        try:
            prompt = _render_prompt(messages, provider, announced_thresholds)
            if queued_inputs:
                user_input = queued_inputs.pop(0).strip()
                image_paths = []
                # Echo the queued line so the user sees what's running now.
                print(format_user_echo(user_input, note="(da fila)"))
            else:
                user_input, image_paths = read_input(prompt)
                user_input = user_input.strip()
                # CODE_REVIEW_FASE1 #6: a fresh user-typed prompt
                # resets the auto-continue budget so a long previous
                # task doesn't silently cap an unrelated new one.
                auto_continues = 0
                # A fresh prompt is a new task → drop the active-skill pin so
                # the next `/<skill>` (or model load_skill) starts clean.
                # Auto-continues (queued_inputs branch above) preserve the pin
                # so the model stays blocked from chaining mid-task.
                clear_active_skill()
        except (KeyboardInterrupt, EOFError):
            # Auto-save on exit
            if len(messages) > 1:
                save_session(session_id, messages, {"provider": provider, "model": cfg["model"]})
                print(f"\n  {c(C.GRAY, f'Session saved: {session_id}')}")
            print(f"{c(C.GRAY, 'Goodbye.')}")
            break

        if not user_input and not image_paths:
            continue

        # One-shot agent dispatch: "@name message"
        if _try_at_dispatch(user_input, provider, temperature):
            continue

        # `!cmd` — run a shell command yourself; the transcript rides along
        # with the next message (alpha/bang.py). No approval gate: the user
        # typed it. `!` alone shows the hint.
        if user_input.startswith("!") and not user_input.startswith("!["):
            from alpha import bang as _bang
            if len(user_input.strip()) == 1:
                print(f"  {c(C.YELLOW, 'Usage:')} !<comando>  — roda no seu shell; a saída vai junto na próxima mensagem")
                continue
            try:
                _bang.execute_in_repl(user_input[1:])
            except Exception as e:
                print_error(f"!{user_input[1:].strip()} falhou: {type(e).__name__}: {e}")
            continue

        # Slash command? Dispatch via alpha/cli/commands.
        # Skip when the first token has an embedded `/` so that paths
        # like `/home/foo/bar` fall through to normal input.
        first_token = user_input.split(maxsplit=1)[0]
        if user_input.startswith("/") and "/" not in first_token[1:]:
            ctx = ReplContext(
                messages=messages, history=history, session_id=session_id,
                provider=provider, temperature=temperature, cfg=cfg,
                system_prompt=system_prompt, tools=tools,
                get_tool_fn=get_tool_fn, active_agent=active_agent,
            )
            from alpha import remote as _remote
            try:
                with _remote.capture_output(user_input):
                    result = dispatch(ctx, user_input)
            except (KeyboardInterrupt, EOFError, SystemExit):
                # #D809: caminhos de saída do REPL nunca são engolidos —
                # EOFError é subclasse de Exception e cairia no guard abaixo.
                raise
            except Exception as e:
                # AUDIT_V2.3 #700(b): nenhum handler de slash-command pode
                # derrubar o REPL — /load de sessão malformada era o gatilho
                # concreto (KeyError em message sem `role`). Loga, avisa e
                # segue para o próximo prompt.
                print_error(f"Comando {first_token} falhou: {type(e).__name__}: {e}")
                result = DispatchResult.CONTINUE
            # Pull mutable state back from ctx — handlers may have rebound
            # session_id, provider, cfg, agent, etc.
            messages, history = ctx.messages, ctx.history
            session_id = ctx.session_id
            provider, temperature, cfg = ctx.provider, ctx.temperature, ctx.cfg
            system_prompt, tools, get_tool_fn = ctx.system_prompt, ctx.tools, ctx.get_tool_fn
            active_agent = ctx.active_agent

            if result is DispatchResult.BREAK:
                break
            if result is DispatchResult.CONTINUE:
                continue
            # FALL_THROUGH — handler transformed the input. Use the
            # override values for the LLM call below.
            if ctx.user_input_override is not None:
                user_input = ctx.user_input_override
            if ctx.image_paths_override is not None:
                image_paths = ctx.image_paths_override
            _history_record_override = ctx.history_record_override
        else:
            _history_record_override = None

        # Inject CWD context (+ transcripts of `!` commands run since the
        # last message; the history record below keeps the raw input).
        cwd = os.getcwd()
        from alpha import bang as _bang
        _shell_ctx = _bang.drain_context()
        _llm_input = f"{_shell_ctx}\n\n{user_input}" if _shell_ctx else user_input
        contextualized = _apply_plan_mode_note(f"[CWD: {cwd}]\n{_llm_input}")
        contextualized, image_paths = _attach_ocr_if_needed(
            contextualized, image_paths, cfg, provider
        )
        user_content = build_user_content(
            contextualized, image_paths, vision_format=cfg.get("vision_format", "openai")
        )
        if image_paths:
            print(c(C.GRAY, f"  ({len(image_paths)} image(s) attached)"))

        # User-prompt hook (non-blocking; output goes to stderr/log)
        try:
            hooks.fire(
                "on_user_prompt",
                user_prompt=user_input,
                workspace=active_agent.workspace if active_agent else None,
            )
        except Exception:
            pass

        messages.append({"role": "user", "content": user_content})
        from alpha import remote as _remote
        if _remote.is_active():
            _remote.set_info(model=cfg.get("model"), cwd=cwd)
            _remote.publish_user(_history_record_override or user_input)
        # `/image` rewrites the history entry to include the filename so
        # /history doesn't show a giant base64 blob; other commands fall
        # through with override=None and we just record the raw input.
        history.append(
            {"role": "user",
             "content": _history_record_override or user_input}
        )

        print()
        # Clear the pinned summary so the bottom_toolbar doesn't display
        # the previous turn's metrics next to the spinner of the new one.
        from alpha.display import clear_last_turn_summary
        clear_last_turn_summary()
        ci_started = repl_concurrent.start()
        interrupted = False
        # Plan mode (camada 1): o modelo só vê tools read-only + present_plan.
        _plan_at_turn_start = plan_mode.is_active()
        turn_tools = (
            plan_mode.filter_openai_tools(tools, get_tool_fn)
            if _plan_at_turn_start
            else tools
        )
        try:
            reply, _turn_anomaly = asyncio.run(
                _run_once(
                    messages, user_input, provider, temperature,
                    get_tool_fn, turn_tools,
                    workspace=active_agent.workspace if active_agent else None,
                )
            )
        except KeyboardInterrupt:
            print(c(C.YELLOW, "\n\nInterrupted."))
            reply = ""
            interrupted = True
        finally:
            if ci_started:
                repl_concurrent.stop()
        # Plano aprovado durante o turno (approval_callback desliga o modo):
        # enfileira a execução — o próximo ciclo roda com as tools completas.
        if _plan_at_turn_start and not plan_mode.is_active() and not interrupted:
            queued_inputs.append(
                "O plano foi aprovado. Execute o plano agora, passo a passo."
            )
        if interrupted:
            # Ctrl+C clears every pending input — both the cross-turn
            # queue and anything still sitting in the reader's deque.
            # Otherwise a panic-cancel would silently re-run the very
            # commands the user was trying to abort.
            queued_inputs.clear()
            repl_concurrent.drain_queue()
            _remote.clear_inbox()
        else:
            # Pull anything the user typed while the agent was working
            # so the next loop iteration runs those before re-opening
            # the prompt.
            queued_inputs.extend(repl_concurrent.drain_queue())
        print()

        # ── Auto-continue marker handling ──
        # The model can split big work across turns by ending its reply
        # with `[CONTINUE: next step description]` on its own line. We
        # extract that suffix, STRIP it from the persisted reply
        # (CODE_REVIEW_FASE1 #4), and queue the inner text as the next
        # user input. Capped per session (ALPHA_AUTO_CONTINUE_MAX).
        next_step: str | None = None
        if reply and not interrupted:
            next_step, reply = _extract_continue(reply)

        if reply:
            messages.append({"role": "assistant", "content": reply})
            history.append({"role": "assistant", "content": reply})

        # Desktop nudge for long turns (alpha.notify decides: mode, TTY,
        # ALPHA_NOTIFY_AFTER threshold). Never raises.
        try:
            from alpha import notify as _notify
            _notify.turn_finished(reply or "")
        except Exception:  # pragma: no cover - defensive
            pass

        # NOTE: a turn now runs to completion in ONE continuous loop (up to
        # max_iterations) and forces a final summary if a cap is ever hit
        # (see alpha.agent.run_agent), so there is no "resumable-cut
        # auto-continue" — the V1.8 behavior that silently re-queued "continue"
        # and restarted the agent, which never converged on models that don't
        # emit `[CONTINUE:]`. Explicit `[CONTINUE: …]` from the model still
        # chains turns intentionally; the user can always type "continue".
        if next_step:
            auto_continues = _handle_auto_continue(
                next_step, auto_continues, _auto_continue_max, queued_inputs
            )


def _auto_continue_limit() -> int:
    """Cap de auto-continues da sessão (ALPHA_AUTO_CONTINUE_MAX, mínimo 1)."""
    from alpha.config import _env_int_clamped

    return _env_int_clamped("ALPHA_AUTO_CONTINUE_MAX", _AUTO_CONTINUE_MAX_DEFAULT)


def _resolve_resume_messages(resume: str) -> tuple[str, list[dict]] | None:
    """Resolve `--continue`/`--resume` para (session_id, mensagens salvas).

    `resume == "__last__"` (flag --continue) usa a última sessão salva.
    Retorna None quando não há sessão correspondente.
    """
    sid = get_last_session_id() if resume == "__last__" else resume
    if not sid:
        return None
    loaded = load_session(sid)
    if not loaded:
        return None
    return sid, loaded


def run_single(provider: str, temperature: float, message: str, resume: str | None = None):
    """Single command mode (non-interactive)."""
    active_agent, provider, temperature, system_prompt, get_tool_fn, tools = \
        _setup_agent_session(provider, temperature)

    prior: list[dict] = []
    if resume:
        resolved = _resolve_resume_messages(resume)
        if resolved is None:
            print_error(
                "Sessão anterior não encontrada para --continue/--resume."
            )
            sys.exit(1)
        sid, prior = resolved
        print(c(C.GRAY, f"  Retomando sessão {sid} ({len(prior)} mensagens)"))

    cwd = os.getcwd()
    contextualized = f"[CWD: {cwd}]\n{message}"
    messages = [
        {"role": "system", "content": system_prompt},
        *prior,
        {"role": "user", "content": contextualized},
    ]

    try:
        reply, _ = asyncio.run(
            _run_once(
                messages, message, provider, temperature,
                get_tool_fn, tools,
                workspace=active_agent.workspace if active_agent else None,
            )
        )
    except KeyboardInterrupt:
        print(c(C.YELLOW, "\nInterrupted."))
        sys.exit(1)

    # CODE_REVIEW_FASE1 #15: single-shot mode doesn't loop, so the
    # `[CONTINUE: …]` marker is unactionable. Strip it from the printed
    # reply (already happens upstream in _run_once via flush_text) and
    # emit a hint so CI/script users know they hit a continuation but
    # got only the first chunk.
    if reply:
        next_step, _ = _extract_continue(reply)
        if next_step:
            print(
                c(C.YELLOW, f"\n  ⚠ Modelo solicitou continuação ([CONTINUE: {next_step}])\n"
                            f"     run_single roda 1 turno só; use REPL (`alpha` sem args) para multi-turn.")
            )

    if not reply:
        sys.exit(1)


def run_print(
    provider: str,
    temperature: float,
    message: str,
    output_format: str = "text",
    max_turns: int = 1,
    auto_approve: bool = False,
    resume: str | None = None,
):
    """Headless print mode (`alpha -p`): sem prompts, saída limpa no stdout.

    Pensado para scripts/CI: aprovações nunca bloqueiam — tools que
    exigiriam prompt são negadas por default, e `--auto-approve` libera
    todas. Atenção: o tier auto-aprovado do Alpha (write_file/edit_file no
    workspace, shell allowlistado) executa SEM consultar a política, mesmo
    sem `--auto-approve` — o mesmo modelo de confiança do REPL (ver
    docstring de `alpha.sdk`). Para bloqueá-lo também, use plan mode ou
    regras `deny` em `.alpha/settings.json`. Logs de setup vão para stderr
    quando a saída é JSON, e `--max-turns` encadeia turnos via `[CONTINUE:]`.
    """
    import contextlib
    import json as _json

    from alpha import sdk

    json_output = output_format in ("json", "stream-json")

    # Setup (MCP/skills/banner de agente) imprime no stdout — em modo JSON
    # isso poluiria a saída consumível; redireciona para stderr.
    setup_redirect = (
        contextlib.redirect_stdout(sys.stderr) if json_output else contextlib.nullcontext()
    )
    with setup_redirect:
        active_agent, provider, temperature, system_prompt, get_tool_fn, tools = \
            _setup_agent_session(provider, temperature)

    prior: list[dict] = []
    if resume:
        resolved = _resolve_resume_messages(resume)
        if resolved is None:
            print("Sessão anterior não encontrada para --continue/--resume.", file=sys.stderr)
            sys.exit(1)
        _, prior = resolved

    # `alpha -p "/review <target> [--deep]"` (CI / scripts): the diff is
    # collected here exactly like the REPL command, then reviewed headless.
    try:
        from alpha.cli.commands.review_cmd import expand_headless
        expanded = expand_headless(message, os.getcwd())
    except ValueError as e:  # ReviewError
        print(str(e), file=sys.stderr)
        sys.exit(2)
    if expanded is not None:
        if not expanded:
            print("Nada para revisar.", file=sys.stderr)
            sys.exit(0)
        message = expanded

    messages = [{"role": "system", "content": system_prompt}, *prior]
    contextualized = f"[CWD: {os.getcwd()}]\n{message}"

    # Fronteira do /rewind: o modo print não passa por _run_once, então marca
    # o turno aqui — edições desta invocação não viram "o último turno" de um
    # REPL futuro no mesmo workspace.
    from alpha import checkpoints as _checkpoints
    _checkpoints.mark_turn(message)

    on_event = None
    if output_format == "stream-json":
        def on_event(event: dict) -> None:
            try:
                print(_json.dumps(event, ensure_ascii=False, default=str), flush=True)
            except (TypeError, ValueError):
                pass

    try:
        result = sdk.run_task_sync(
            contextualized,
            provider=provider,
            temperature=temperature,
            workspace=active_agent.workspace if active_agent else None,
            approval="auto" if auto_approve else "deny",
            max_turns=max_turns,
            messages=messages,
            on_event=on_event,
            _tools_openai=tools,
            _get_tool_fn=get_tool_fn,
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(1)

    # Background sub-agent child (delegate_task(background=true)): leave the
    # final conversation where the parent's agent_message can revive it.
    from alpha import bg_resume as _bg_resume
    _bg_target = _bg_resume.child_target()
    if _bg_target is not None:
        _bg_resume.save_transcript(
            _bg_target, result.messages,
            task=message, status="error" if result.error else "completed",
            reply=result.reply, agent=os.environ.get("ALPHA_AGENT", ""),
            provider=provider,
        )

    if output_format == "json":
        tool_calls = [
            e.get("name", "") for e in result.events if e.get("type") == "tool_call"
        ]
        print(_json.dumps(
            {
                "reply": result.reply,
                "turns": result.turns,
                "tool_calls": tool_calls,
                "cost_usd": round(result.cost_usd, 6),
                "error": result.error,
            },
            ensure_ascii=False,
        ))
    elif output_format == "stream-json":
        pass  # eventos já foram emitidos linha a linha
    else:
        if result.reply:
            print(result.reply)
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)

    sys.exit(0 if result.ok else 1)


def main():
    # `alpha skills ...` has its own subparser (alpha/cli/skills_cli.py)
    # so the main flat CLI stays uncluttered. Peek at argv before argparse
    # touches it; --help/--version still flow through the main parser.
    if len(sys.argv) > 1 and sys.argv[1] == "skills":
        from alpha.cli.skills_cli import main as skills_main
        sys.exit(skills_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        description="Alpha Code — Standalone terminal agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python main.py                                    # interactive mode
              python main.py "create a hello world"             # single command
              python main.py --provider grok "analyze this project"
              python main.py --provider ollama "list files"
        """),
    )
    parser.add_argument(
        "message", nargs="?", help="Message to send (interactive mode if omitted)"
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        help=f"LLM provider: deepseek, openai, grok, ollama (default: {DEFAULT_PROVIDER})",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.5,
        help="LLM temperature (default: 0.5)",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="List available providers and exit",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Run the onboarding wizard (configure provider, API key, workspace)",
    )
    parser.add_argument(
        "--pick",
        action="store_true",
        help="Interactively choose the provider/model at startup (REPL only)",
    )
    parser.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="Resume the last saved session (REPL or single-shot)",
    )
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a specific saved session (see /sessions for IDs)",
    )
    parser.add_argument(
        "-p",
        "--print",
        dest="print_mode",
        action="store_true",
        help="Headless print mode: no prompts, clean stdout (reads stdin if no message)",
    )
    parser.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        default="text",
        help="Print-mode output: text (default), json (single object), stream-json (event lines)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=1,
        metavar="N",
        help="Print mode: chain up to N turns via the [CONTINUE:] marker (default: 1)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Print mode: approve tools that would otherwise prompt (default: "
            "those are denied). Note: file writes/edits and allowlisted shell "
            "are auto-approved and run even without this flag. Use with care."
        ),
    )

    parser.add_argument(
        "--remote",
        nargs="?",
        const="lan",
        choices=("lan", "local"),
        help=(
            "REPL: start remote control — a page on your network (token link) "
            "to follow and drive this session from a phone. `--remote local` "
            "binds to 127.0.0.1 only (use with your own tunnel)."
        ),
    )

    args = parser.parse_args()

    if args.init:
        from alpha.wizard import run_wizard

        sys.exit(0 if run_wizard() else 1)

    if args.list_providers:
        print_providers_list(get_available_providers())
        return

    if args.pick and not args.message:
        args.provider = _pick_provider_interactive(args.provider)

    # Validate provider (called for its side-effect: raises on bad config)
    try:
        get_provider_config(args.provider)
    except RuntimeError as e:
        print_error(str(e))
        sys.exit(1)

    resume = args.resume or ("__last__" if args.continue_ else None)

    if args.print_mode:
        message = args.message
        if not message and not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        if not message:
            print_error("Modo print (-p) requer uma mensagem (argumento ou stdin).")
            sys.exit(1)
        run_print(
            args.provider,
            args.temperature,
            message,
            output_format=args.output_format,
            max_turns=args.max_turns,
            auto_approve=args.auto_approve,
            resume=resume,
        )
    elif args.message:
        run_single(args.provider, args.temperature, args.message, resume=resume)
    else:
        run_repl(args.provider, args.temperature, resume=resume, remote=args.remote)


if __name__ == "__main__":
    main()
