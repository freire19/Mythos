"""Public programmatic API (SDK) for embedding the Alpha agent.

Superfície mínima e estável sobre o loop interno `alpha.agent.run_agent`,
para scripts, CI e aplicações que embutem o agente sem o REPL:

    from alpha.sdk import run_task, run_task_sync

    result = await run_task("resuma este repositório", provider="deepseek")
    print(result.reply)

    # ou, em código síncrono:
    result = run_task_sync("liste os TODOs do projeto")

Política de aprovação (`approval`):
- ``"deny"`` (default) — tools destrutivas que exigiriam prompt são
  negadas; o agente adapta e reporta. Seguro para automação.
- ``"auto"`` — aprova tudo (equivalente ao accept-edits da sessão
  inteira). Use apenas em ambiente confiável/sandboxed.
- ``callable(tool_name, args) -> bool`` — política própria do caller.

Nota: tools no tier auto-aprovado do Alpha (write_file/edit_file no
workspace, shell allowlistado) executam sem consultar a política — o
mesmo modelo de confiança do REPL. Para bloqueá-las também, use o plan
mode (`alpha.plan_mode`) ou regras `deny` em `.alpha/settings.json`.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Marcador de continuação emitido pelo modelo para dividir trabalho longo
# em turnos. Fonte canônica — main.py importa daqui.
# CODE_REVIEW_FASE1 #9: o marcador precisa estar sozinho na linha final —
# `(?:^|\n)` + `\s*\Z` impedem que um exemplo inline em texto de
# documentação ("use `[CONTINUE: foo]`…") dispare auto-continue.
CONTINUE_MARKER_RE = re.compile(
    r"(?:^|\n)\s*\[CONTINUE:\s*([^\]\n]+?)\s*\]\s*\Z",
)


def extract_continue(reply: str) -> tuple[str | None, str]:
    """Pull a trailing `[CONTINUE: …]` marker off `reply`.

    Returns ``(next_step, reply_without_marker)``. When no marker is
    present, ``(None, reply)``. The returned reply has trailing
    whitespace trimmed so callers can persist it cleanly to history
    without the marker leaking into the next turn's LLM context.
    """
    stripped = reply.rstrip()
    m = CONTINUE_MARKER_RE.search(stripped)
    if not m:
        return None, reply
    next_step = m.group(1).strip()
    if not next_step:
        return None, reply
    return next_step, stripped[: m.start()].rstrip()


@dataclass
class TaskResult:
    """Resultado de `run_task`.

    `events` guarda os eventos não-token (tool_call, tool_result, error,
    anomaly, context_compressed) na ordem em que ocorreram; tokens são
    apenas repassados ao callback `on_event` para não inflar memória.
    """

    reply: str = ""
    messages: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    turns: int = 0
    error: str | None = None
    cost_usd: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.reply)


def _resolve_approval(policy) -> Callable[[str, dict], bool]:
    if callable(policy):
        return policy
    if policy == "auto":
        return lambda _name, _args: True
    if policy == "deny":
        return lambda _name, _args: False
    raise ValueError(
        f"approval inválido: {policy!r} (use 'deny', 'auto' ou um callable)"
    )


async def run_task(
    task: str,
    *,
    provider: str | None = None,
    temperature: float = 0.5,
    system_prompt: str | None = None,
    workspace: str | None = None,
    tools: list[str] | None = None,
    approval: str | Callable[[str, dict], bool] = "deny",
    max_iterations: int | None = None,
    max_turns: int = 1,
    messages: list[dict] | None = None,
    on_event: Callable[[dict], None] | None = None,
    _tools_openai: list[dict] | None = None,
    _get_tool_fn: Callable | None = None,
) -> TaskResult:
    """Roda uma tarefa no loop do agente e retorna o resultado agregado.

    Args:
        task: A tarefa em linguagem natural.
        provider: Provider LLM (default: o configurado no ambiente).
        system_prompt: Override do system prompt (default: o bundled).
        workspace: Raiz de trabalho para enforcement de paths.
        tools: Filtro de nomes de tools a oferecer (None = todas).
        approval: Política de aprovação — ver docstring do módulo.
        max_iterations: Override do limite de iterações por turno.
        max_turns: Turnos encadeados via marcador `[CONTINUE: …]` (>= 1).
        messages: Conversa existente para continuar (lista OpenAI-format,
            SEM a nova mensagem user — ela é montada a partir de `task`).
            Uma mensagem system em messages[0] é preservada.
        on_event: Callback síncrono chamado para CADA evento do loop
            (inclusive tokens) — use para streaming/telemetria.
        _tools_openai/_get_tool_fn: Overrides avançados (schemas prontos,
            ex.: incluindo MCP carregado pela CLI). Ignoram `tools`.
    """
    from .agent import run_agent
    from ._turn_budget import reset as _reset_deadline, set_deadline_capped
    from .config import DEFAULT_PROVIDER, MAX_TURN_SECONDS, load_system_prompt
    from .cost import session_cost_usd

    provider = provider or DEFAULT_PROVIDER
    approval_cb = _resolve_approval(approval)
    max_turns = max(1, int(max_turns))

    if _tools_openai is not None:
        openai_tools = _tools_openai
        get_tool_fn = _get_tool_fn
    else:
        from .tools import get_openai_tools, get_tool, load_all_tools

        load_all_tools()
        name_filter = None
        if tools is not None:
            wanted = set(tools)
            name_filter = lambda names: [n for n in names if n in wanted]  # noqa: E731
        openai_tools = get_openai_tools(name_filter=name_filter)
        get_tool_fn = get_tool

    # #D319: um sub-agente headless de background (ALPHA_SUBAGENT_HEADLESS=1,
    # setado por delegate_task(background=true)) aplica a
    # SUBAGENT_DESTRUCTIVE_BLOCKLIST do contrato in-process — o filho detached
    # não deve ganhar execute_shell/execute_python/http_request/browser-write
    # que o caminho in-process bloqueia. Fecha a cadeia write_file+run.
    if os.environ.get("ALPHA_SUBAGENT_HEADLESS") == "1":
        from .tools._delegate_core import SUBAGENT_DESTRUCTIVE_BLOCKLIST as _sub_blk
        openai_tools = [
            t for t in openai_tools
            if t.get("function", {}).get("name") not in _sub_blk
        ]
        _inner_get_tool = get_tool_fn

        def get_tool_fn(_name):  # noqa: E306
            return None if _name in _sub_blk else _inner_get_tool(_name)

    if messages is not None and messages and messages[0].get("role") == "system":
        convo = list(messages)
    else:
        if system_prompt is None:
            # Same read path the REPL uses (cli/setup.build_system_prompt) —
            # an embedder that passes its own prompt opts out implicitly.
            from .memory import inject_memory
            system_prompt = inject_memory(load_system_prompt())
        convo = [{"role": "system", "content": system_prompt}]
        if messages:
            convo.extend(messages)

    result = TaskResult(messages=convo)
    cost_before = session_cost_usd()
    current_task = task

    for _turn in range(max_turns):
        convo.append({"role": "user", "content": current_task})
        result.turns += 1
        reply = ""
        # Deadline de turno no ContextVar (espelha main._run_once) para o
        # clamp por-tool e o teto do stream valerem também no headless/SDK;
        # `set_deadline_capped` preserva um deadline externo mais apertado.
        _td_token = set_deadline_capped(time.monotonic() + MAX_TURN_SECONDS)
        try:
            async for event in run_agent(
                convo,
                current_task,
                temperature=temperature,
                provider=provider,
                get_tool_fn=get_tool_fn,
                tools=openai_tools,
                approval_callback=approval_cb,
                max_iterations=max_iterations,
                workspace=workspace,
                bg_notifications=False,
            ):
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        pass  # telemetria do caller nunca derruba a tarefa
                etype = event.get("type")
                if etype == "done":
                    reply = event.get("reply", "") or ""
                elif etype == "error":
                    result.error = event.get("message", "unknown error")
                    result.events.append(event)
                elif etype not in ("token", "thinking", "stream_reset"):
                    result.events.append(event)
        finally:
            _reset_deadline(_td_token)

        next_step, reply = extract_continue(reply)
        if reply:
            convo.append({"role": "assistant", "content": reply})
            result.reply = reply
        if result.error or not next_step:
            break
        current_task = next_step

    result.cost_usd = max(0.0, session_cost_usd() - cost_before)
    return result


def run_task_sync(task: str, **kwargs) -> TaskResult:
    """Wrapper síncrono de `run_task` (cria o event loop)."""
    import asyncio

    return asyncio.run(run_task(task, **kwargs))
