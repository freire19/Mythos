"""
Tool executor for Mythos.

Executes tool calls with parallel support, handles approval flow, formats results.
When multiple tools are called in the same turn, independent tools run in parallel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncGenerator, Callable
from itertools import islice
from typing import NamedTuple

from . import hooks
from ._text_safety import strip_bidi
from .stats import record_approval, record_tool
from .config import (
    SLOW_TOOL_TIMEOUT as _CFG_SLOW_TOOL_TIMEOUT,
    TOOL_EXECUTION_TIMEOUT as _CFG_TOOL_EXECUTION_TIMEOUT,
    TOOL_RESULT_MAX_CHARS,
)

logger = logging.getLogger(__name__)

# #D003 (V1.0): re-export por retro-compat. O codigo legacy (composite_tools,
# tests) importava daqui. Fonte unica de verdade vive em `alpha.config`.
TOOL_EXECUTION_TIMEOUT = _CFG_TOOL_EXECUTION_TIMEOUT
_SLOW_TOOL_TIMEOUT = _CFG_SLOW_TOOL_TIMEOUT
_SLOW_TOOLS = frozenset({
    "investigate_person", "deploy_check", "run_tests",
    "delegate_task", "delegate_parallel", "delegate_consensus",
    "execute_shell", "execute_pipeline",
    "browser_open", "browser_navigate", "browser_get_content",
    "browser_describe_page", "browser_screenshot",
    "browser_click", "browser_fill", "browser_wait_for",
    "browser_new_tab",
})

_DELEGATE_TOOLS = frozenset({
    "delegate_task", "delegate_parallel", "delegate_consensus",
})
_DELEGATE_TIMEOUT_MARGIN = 60.0


def _delegate_timeout(tool_name: str, args: dict) -> float:
    """Timeout externo dos `delegate_*` derivado dos budgets internos.

    `ceil(n_tarefas / max_parallel)` ondas × `subagent_max_turn_seconds`
    + margem (scratch dir, hooks, gather, clustering), nunca abaixo de
    `_SLOW_TOOL_TIMEOUT`. Um timeout fixo menor que o wall-clock das ondas
    cancelaria o gather e descartaria até sub-agentes já concluídos.
    """
    from .config import FEATURES

    budget = float(FEATURES.get("subagent_max_turn_seconds", 120.0))
    max_parallel = max(1, int(FEATURES.get("max_parallel_agents", 3)))
    n = 1
    if tool_name == "delegate_parallel":
        try:
            parsed = json.loads(args.get("tasks") or "[]")
        except (json.JSONDecodeError, TypeError):
            parsed = []
        if isinstance(parsed, list) and parsed:
            n = min(len(parsed), int(FEATURES.get("max_delegate_total_tasks", 10)))
    elif tool_name == "delegate_consensus":
        raw_n = args.get("n", 3)
        if isinstance(raw_n, int) and not isinstance(raw_n, bool) and raw_n > 0:
            n = min(raw_n, max_parallel)
        else:
            n = max_parallel
    waves = math.ceil(n / max_parallel)
    return max(float(_SLOW_TOOL_TIMEOUT), waves * budget + _DELEGATE_TIMEOUT_MARGIN)


def _no_deny(_tool: str, _args: dict) -> tuple[bool, str]:
    return False, ""


def build_assistant_tool_message(
    content: str | None,
    tool_calls: list[dict],
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict:
    """Build an OpenAI-compatible assistant message containing tool_calls.

    `reasoning_content` (DeepSeek-reasoner thinking-mode tokens) deve ser
    devolvido na proxima request ou o provider responde HTTP 400. Quando
    nao usado (qualquer outro modelo), o caller passa None e o campo nao
    e adicionado — providers que nao reconhecem o campo simplesmente
    ignoram, mas omitir e mais limpo.

    `thinking_blocks` (extended thinking do Anthropic) segue o mesmo
    contrato: blocos verbatim (com signature) que o adapter re-emite antes
    do tool_use na proxima request (llm_anthropic._convert_messages).
    """
    msg: dict = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
                # Gemini's OpenAI-compat layer puts a `thought_signature`
                # under `extra_content.google` on each tool_call and
                # requires it to be echoed back, or replies HTTP 400
                # INVALID_ARGUMENT on the next turn. Other providers
                # ignore the extra field, so it's safe to forward when
                # present.
                **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {}),
            }
            for tc in tool_calls
        ],
    }
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


_PREVIEW_FIELD_MAX = 1000
# Issue #005 (AUDIT): o clip antigo mantinha só o INÍCIO de cada campo — em
# logs/stack traces o erro está no FINAL, então a truncagem cortava exatamente
# a informação de diagnóstico. Head+tail preserva as duas pontas com um
# marcador explícito do que foi omitido.
_PREVIEW_HEAD = 600
_PREVIEW_TAIL = _PREVIEW_FIELD_MAX - _PREVIEW_HEAD


def _clip_field(v: str) -> str:
    """Clip a string field keeping head AND tail, with an explicit marker."""
    if len(v) <= _PREVIEW_FIELD_MAX:
        return v
    omitted = len(v) - _PREVIEW_HEAD - _PREVIEW_TAIL
    return (
        v[:_PREVIEW_HEAD]
        + f"\n... [{omitted} chars omitted — middle of this field] ...\n"
        + v[-_PREVIEW_TAIL:]
    )


def _clip_preview_value(v):
    """Clip a preview field: strings via _clip_field; non-string values
    bigger than the per-field budget become a placeholder instead of being
    serialized whole (#DP037)."""
    if isinstance(v, str):
        return _clip_field(v)
    est = _cheap_len(v)
    if est > _PREVIEW_FIELD_MAX:
        return f"<{type(v).__name__} omitido — {est} chars est.>"
    return v


# #DM075: tamanho da amostra de extrapolação de `_cheap_len` — antes um
# literal `50` repetido em 6 sites que precisavam mudar em lockstep.
_CHEAP_LEN_SAMPLE = 50


def _cheap_len(v) -> int:
    """Estimativa rápida do tamanho serializado sem alocar string gigante.

    DEEP_PERFORMANCE #D030: `len(str(v))` em dicts/listas aninhados
    materializa a representação inteira só para medir. Para coleções,
    amostramos os primeiros `_CHEAP_LEN_SAMPLE` itens e extrapolamos.
    Em dicts usamos `islice` para nao alocar O(N) tuplas (#P003).

    DR007: a margem absoluta `+100` era irrelevante em coleções grandes
    (lista de 10k itens com sample de 50KB ficaria com erro >> 100 chars
    de margem). Substituido por margem proporcional 5% do extrapolado +
    100 absoluto. Cobre viés de amostragem onde os primeiros 50 itens
    são menores que a média da coleção; ainda subestima em distribuições
    fortemente skewed, mas o consumidor (_format_result) corta strings
    por campo depois de detectar excesso — qualquer subestima vira corte
    extra, nao corrupção.
    """
    def _with_margin(extrapolated: int) -> int:
        return extrapolated + (extrapolated // 20) + 100  # 5% + 100

    if isinstance(v, str):
        return len(v)
    if isinstance(v, (list, tuple)):
        if len(v) <= _CHEAP_LEN_SAMPLE:
            return sum(_cheap_len(x) for x in v)
        sample = sum(_cheap_len(x) for x in v[:_CHEAP_LEN_SAMPLE])
        return _with_margin(sample * len(v) // _CHEAP_LEN_SAMPLE)
    if isinstance(v, dict):
        n = len(v)
        if n <= _CHEAP_LEN_SAMPLE:
            return sum(_cheap_len(k) + _cheap_len(val) + 4 for k, val in v.items())
        sample = sum(
            _cheap_len(k) + _cheap_len(val) + 4
            for k, val in islice(v.items(), _CHEAP_LEN_SAMPLE)
        )
        return _with_margin(sample * n // _CHEAP_LEN_SAMPLE)
    return len(str(v))


# DEEP_SECURITY V3.3 #007: cache do home path para reescrita em tool results.
# Se `expanduser("~")` falhar (sem HOME env, container minimal), `_HOME_PATH`
# fica vazio e a sanitizacao vira no-op em vez de quebrar.
_HOME_PATH = os.path.expanduser("~")
if _HOME_PATH in ("~", "", "/"):
    _HOME_PATH = ""


def _sanitize_paths(text: str) -> str:
    """Substituir $HOME absoluto por `~` antes de mandar tool result ao LLM.

    Provider LLM nao precisa do username do usuario em todo path; `~`
    preserva informacao topologica ("esta sob home") sem vazar identidade.
    Hot-path: short-circuit quando home nao esta no texto.
    """
    if not _HOME_PATH or _HOME_PATH not in text:
        return text
    return text.replace(_HOME_PATH, "~")


# #D136: bidi/zero-width DIRECTIONAL override chars. Untrusted tool results
# (web_search, extract_page_content, query_database) can embed RLO/LRO/PDI to
# visually reorder instructions when shown as a tool result — a prompt-injection
# amplifier. The </tool_result> close-tag is already neutralized; this closes
# the content gap. Policy única em `alpha._text_safety` (#501) — a variante
# bidi-only preserva o ZWNJ que o neutralizador de close-tag usa.
_strip_bidi = strip_bidi


def _format_result(result: dict, tool_name: str) -> str:
    """Truncate and format a tool result for inclusion in messages.

    Hot path: _cheap_len estima o tamanho serializado sem json.dumps
    completo. Se passar do limite, strings sao cortadas por campo em vez
    de fatiar JSON serializado bruto — a versao antiga fatiava no meio
    do JSON e podia cortar em `\\uXXXX` ou em multi-byte UTF-8, produzindo
    saida com texto corrompido.
    """
    # Drop underscore-prefixed keys: convention for UI-only fields (e.g.
    # `_previous_content` used to render the edit diff in the terminal,
    # but pointless and bloating to send back to the LLM).
    result = {k: v for k, v in result.items() if not (isinstance(k, str) and k.startswith("_"))}
    estimated = sum(_cheap_len(v) for v in result.values()) + 100
    if estimated <= TOOL_RESULT_MAX_CHARS:
        return _strip_bidi(_sanitize_paths(json.dumps(result, ensure_ascii=False)))

    preview = {k: _clip_preview_value(v) for k, v in result.items()}
    truncated = {
        "truncated": True,
        "tool": tool_name,
        "preview": preview,
        "message": (
            f"Result truncated (estimated {estimated} chars > "
            f"{TOOL_RESULT_MAX_CHARS}); long string fields keep their first "
            f"{_PREVIEW_HEAD} and last {_PREVIEW_TAIL} chars (errors usually "
            "sit at the end). Re-run the tool with offset/limit or a narrower "
            "query for the omitted middle."
        ),
    }
    out = json.dumps(truncated, ensure_ascii=False)
    if len(out) > TOOL_RESULT_MAX_CHARS:
        # AUDIT_V1.2 #019: slicing raw JSON mid-string produces invalid JSON
        # (unbalanced braces/quotes), which causes provider HTTP 400.
        # Replace with a minimal valid JSON object.
        minimal = {
            "truncated": True,
            "tool": tool_name,
            "message": (
                f"Result too large even after preview truncation "
                f"(>{TOOL_RESULT_MAX_CHARS} chars). Use offset/limit args "
                f"on this tool or split the work."
            ),
        }
        out = json.dumps(minimal, ensure_ascii=False)
    return _strip_bidi(_sanitize_paths(out))


def _tc_id(tc: dict) -> str:
    """#D108: tool-call id, tolerant of a tc that omits it. The OpenAI path
    always sets `id` (llm.py), but the Anthropic adapter or a hand/test-built
    tc might not — a bracket index would KeyError and crash the whole batch
    instead of degrading to an empty id."""
    return tc.get("id", "") or ""


def _tc_name(tc: dict) -> str:
    """#D108: tool-call name, tolerant of a missing field (see `_tc_id`). An
    empty name falls through to the unknown-tool error path in
    `_validate_tool_call`."""
    return tc.get("name", "") or ""


def _annotate_error(result: dict, category: str) -> dict:
    """Adicionar invariante `{ok: false, category}` em resultados de erro.

    DEEP_LOGIC #DL017: errors apareciam em N formatos diferentes
    (`{"error": ...}`, `{"skipped": True, ...}`, `{"workspace_violation": True, ...}`),
    forcando o modelo a aprender variantes pra detectar falha. Mantemos os
    campos legados pra retrocompatibilidade mas garantimos que toda falha
    tem `ok=false` + `category` em uma das categorias conhecidas:
    `denied`, `timeout`, `violation`, `runtime`, `parse_error`, `unknown_tool`.
    """
    out = dict(result)
    out.setdefault("ok", False)
    out.setdefault("category", category)
    return out


def _append_tool_msg(messages: list[dict], tc_id: str, result: dict, tool_name: str) -> None:
    """Append a tool result message with truncation + UTF-8 safe formatting.

    Centraliza o `messages.append({...content: ...})` para que erro paths
    e success paths usem o mesmo `_format_result` (truncamento, preview por
    campo). Sem este helper, error messages com paths absolutos longos
    podiam passar de TOOL_RESULT_MAX_CHARS (#D022).

    DEEP_SECURITY V3.3 #D125: `json.dumps(ensure_ascii=False)` nao escapa `<`
    ou `>` no conteudo — uma string adversarial vinda de web_search,
    extract_page_content, MCP, ou query_database pode conter literalmente
    `</tool_result>` e simular fim do wrapper para o LLM, abrindo vetor de
    indirect prompt injection. Neutralizamos a tag de fechamento embedida
    substituindo `<` por zero-width-non-joiner (U+200C) no token. O resultado
    e visualmente identico mas a regex que o LLM usa para delimitar o
    tool_result nao casa mais.
    """
    content = _format_result(result, tool_name)
    # Escape any `</tool_result>` (case-insensitive) appearing inside the
    # content so the wrapper boundary stays unambiguous. DEEP_PERFORMANCE
    # #DP026: the old `if "</tool_result>" in content.lower()` guard allocated
    # a full lowercase copy (up to TOOL_RESULT_MAX_CHARS, ~12 KB) on EVERY tool
    # call just to decide whether to neutralize. The compiled regex below scans
    # in C and returns the original string untouched when there's no match, so
    # the guard is pure overhead — dropped.
    content = _neutralize_close_tag(content)
    messages.append({
        "role": "tool",
        "tool_call_id": tc_id,
        "content": f"<tool_result>{content}</tool_result>",
    })


_CLOSE_TAG_RE = re.compile(r"</tool_result>", re.IGNORECASE)


def _neutralize_close_tag(content: str) -> str:
    """Replace literal `</tool_result>` with a visually-identical but
    structurally-different sequence (`</tool_result‌>`).

    Case-insensitive: an attacker might inject `</TOOL_RESULT>` to bypass a
    naive case-sensitive replace. DEEP_PERFORMANCE #DP026/#DP022: replaces the
    former char-by-char walk (Python-level loop + lowercase copy of the whole
    string) with a single compiled `re.sub`. We re-emit each match with a
    zero-width-non-joiner before the closing `>`, preserving the attacker's
    original case. When there's no match, `re.sub` returns `content` untouched.
    """
    return _CLOSE_TAG_RE.sub(
        # `m.group(0)[:-1]` is the matched `</tool_result` (original case) sans
        # the final `>`; append ZWNJ + `>` — invisible to the LLM, no regex match.
        lambda m: m.group(0)[:-1] + "‌>",
        content,
    )


def _collect_result_images(result: object) -> list[str]:
    """Extract image paths a tool attached to its result (Plano-Browser E1).

    Convention: `_image_path` (str) or `_image_paths` (list[str]) — the
    underscore prefix keeps them out of the textual JSON sent to the LLM
    (`_format_result` drops `_`-keys), so the image travels ONLY as a vision
    block, never duplicated as a path string.
    """
    if not isinstance(result, dict):
        return []
    paths: list[str] = []
    single = result.get("_image_path")
    if isinstance(single, str) and single:
        paths.append(single)
    many = result.get("_image_paths")
    if isinstance(many, list):
        paths.extend(p for p in many if isinstance(p, str) and p)
    return paths


_VISION_NOTE_PREFIX = "[ALPHA SYSTEM NOTE] Image produced by tool"
_VISION_KEEP_DEFAULT = 3


def _vision_keep_limit() -> int:
    """Quantos screenshots recentes permanecem no contexto (Etapa 1.1).

    Base64 de screenshot custa ~1-2k tokens por imagem A CADA turno; manter
    só os N últimos (default 3) segue o padrão do computer-use da Anthropic.
    ALPHA_VISION_KEEP_IMAGES: int; <= 0 desativa a poda (mantém todos).
    """
    raw = os.environ.get("ALPHA_VISION_KEEP_IMAGES", "").strip()
    try:
        return int(raw) if raw else _VISION_KEEP_DEFAULT
    except ValueError:
        return _VISION_KEEP_DEFAULT


def _prune_old_vision_messages(messages: list[dict], keep: int) -> None:
    """Substitui screenshots antigos (além dos `keep` mais recentes) por uma
    nota textual. Só toca em mensagens que ESTE mecanismo criou (marcadas
    com _VISION_NOTE_PREFIX) — imagens coladas pelo usuário ficam intactas."""
    if keep < 0:
        return
    idxs: list[int] = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list) or not content:
            continue
        first = content[0]
        if (
            isinstance(first, dict)
            and first.get("type") == "text"
            and str(first.get("text", "")).startswith(_VISION_NOTE_PREFIX)
            and any(
                isinstance(b, dict) and b.get("type") in ("image_url", "image")
                for b in content
            )
        ):
            idxs.append(i)
    prune = idxs[:-keep] if keep > 0 else idxs
    for i in prune:
        texts = [
            b.get("text", "") for b in messages[i]["content"]
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        note = texts[0] if texts else _VISION_NOTE_PREFIX
        messages[i]["content"] = (
            note + " [screenshot removed to save context — take a new one if needed]"
        )


def _append_vision_attachments(
    messages: list[dict],
    pending: list[tuple[str, list[str]]],
    provider: str | None,
) -> None:
    """Attach tool-produced images as a vision user message (Plano-Browser E1).

    Appended AFTER all tool messages of the batch: interleaving a user
    message between tool responses of one assistant tool_calls batch is an
    OpenAI-side HTTP 400, and would split the Anthropic adapter's
    tool_result coalescing into two user turns.

    Wire shape is the OpenAI vision block list (same convention as user
    clipboard attachments); the Anthropic adapter converts it. No-op unless
    the active provider supports vision (`vision_tool_results_enabled`).
    """
    if not pending or not provider:
        return
    from .config import vision_tool_results_enabled

    if not vision_tool_results_enabled(provider):
        return
    from pathlib import Path

    from .attachments import build_user_content

    # Etapa 1.1: antes de anexar novos screenshots, poda os antigos para que
    # no máximo `keep` imagens vivam no contexto após este append.
    keep = _vision_keep_limit()
    if keep > 0:
        _prune_old_vision_messages(messages, max(0, keep - len(pending)))

    for tool_name, paths in pending:
        note = (
            f"[ALPHA SYSTEM NOTE] Image produced by tool '{tool_name}' "
            "(auto-attached so you can see it). Use it as ground truth for "
            "what is currently rendered."
        )
        content = build_user_content(note, [Path(p) for p in paths], vision_format="openai")
        if isinstance(content, str):
            # Every image was skipped (missing/oversized — already logged by
            # attachments). Don't append a text-only note the model can't use.
            continue
        messages.append({"role": "user", "content": content})


def _record_skip(tc: dict, tool_name: str, result: dict, messages: list[dict]) -> dict:
    """Append a denied/skipped result to messages and return the event dict."""
    annotated = _annotate_error(result, "denied")
    _append_tool_msg(messages, _tc_id(tc), annotated, tool_name)
    return {"type": "tool_result", "id": _tc_id(tc), "name": tool_name, "result": annotated, "denied": True}


def _record_result(tc: dict, tool_name: str, result: dict, messages: list[dict]) -> None:
    # DL033: tools that return {"error": "..."} on their own (git, db, file
    # tools) don't get the {ok: false, category: ...} invariant added by the
    # executor. Annotate here so the model always sees a consistent error shape.
    if isinstance(result, dict) and "error" in result and "ok" not in result:
        result = _annotate_error(result, "tool_error")
    _append_tool_msg(messages, _tc_id(tc), result, tool_name)


async def _execute_single_tool(tool_def, tool_name: str, args: dict) -> dict:
    """Execute a single tool with timeout. Returns result dict."""
    from ._turn_budget import clamp as _clamp_to_turn_budget, remaining as _budget_remaining

    tool_timeout = (
        _SLOW_TOOL_TIMEOUT if tool_name in _SLOW_TOOLS else TOOL_EXECUTION_TIMEOUT
    )
    if tool_name in _DELEGATE_TOOLS:
        tool_timeout = _delegate_timeout(tool_name, args)

    # CODE_REVIEW2 #2: when the turn budget has already expired, skip
    # the tool call entirely instead of giving it a 1-second floor.
    # Parallel batches would otherwise burn N×1s past the deadline; for
    # delegate_task/delegate_parallel that 1s is also far too short for
    # the LLM to even start, so the call is guaranteed-failed +
    # leaks a partial connection.
    _budget_left = _budget_remaining()
    if _budget_left <= 0:
        logger.warning(
            "Tool %s skipped — turn wall-clock budget exhausted "
            "(deadline already passed)", tool_name,
        )
        return _annotate_error(
            {"error": "Turn wall-clock budget exhausted before tool call started"},
            "timeout",
        )

    # CODE_REVIEW_FASE1 #14: clamp the tool timeout to the remaining
    # turn budget. A 300s slow-tool inside a turn with 25s left times
    # out at 25s and the agent loop emits the max_turn_seconds anomaly
    # cleanly instead of 5+ minutes of silence.
    effective_timeout = _clamp_to_turn_budget(float(tool_timeout))
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            tool_def.executor(**args),
            timeout=effective_timeout,
        )
    except TimeoutError:
        clamped = effective_timeout < tool_timeout
        # CODE_REVIEW2 #6: expected timeouts use logger.warning so the
        # log doesn't drown in tracebacks for routine clamp firings.
        suffix = f" (clamped from {tool_timeout}s by turn budget)" if clamped else ""
        logger.warning(
            "Tool timeout (%s): %.1fs%s",
            tool_name,
            effective_timeout,
            suffix,
        )
        result = _annotate_error(
            {"error": f"Execution timed out ({int(effective_timeout)}s)"}, "timeout"
        )
    except Exception as e:
        logger.exception("Tool error (%s): %s: %s", tool_name, type(e).__name__, e)
        result = _annotate_error(
            {"error": f"{type(e).__name__}: {e}"}, "runtime"
        )
    finally:
        record_tool(tool_name, (time.monotonic() - started) * 1000.0)
    return result


async def _fire_pre_tool(
    tool_name: str, args: dict, workspace: str | None
) -> hooks.HookOutcome:
    # Fast path: skip the thread-hop entirely when no hooks are configured.
    if not hooks.has_event("pre_tool"):
        return hooks.HookOutcome()
    return await asyncio.to_thread(
        hooks.fire,
        "pre_tool",
        tool_name=tool_name,
        tool_args=args,
        workspace=workspace,
    )


async def _fire_post_tool(
    tool_name: str, args: dict, result: dict, workspace: str | None
) -> None:
    if not hooks.has_event("post_tool"):
        return
    await asyncio.to_thread(
        hooks.fire,
        "post_tool",
        tool_name=tool_name,
        tool_args=args,
        workspace=workspace,
        extra={"tool_result": result},
    )


def _enforce_workspace(
    workspace: str | None, tool_name: str, args: dict
) -> tuple[bool, dict, str]:
    """Apply workspace validation to args. Returns (ok, new_args, error_msg)."""
    if not workspace:
        return True, args, ""
    from .agents import validate_workspace_args

    return validate_workspace_args(workspace, tool_name, args)


def _parse_and_validate_args(
    tc: dict, tool_def
) -> tuple[dict | None, dict | None]:
    """Parse JSON args do tool_call e validar contra o schema declarado.

    Retorna `(args, None)` em sucesso, ou `(None, error_result)` em falha
    — `error_result` ja e o dict pronto para virar `tool` message,
    permitindo o modelo se auto-corrigir em vez de receber TypeError opaco.

    Diferente da versao anterior (`args = {}` silencioso em JSONDecodeError),
    qualquer falha vira feedback estruturado. Argumentos extras (fora do
    schema) sao DESCARTADOS silenciosamente para evitar TypeError no `**args`
    do executor — o modelo se auto-corrige na proxima iteracao se o resultado
    indicar que campo faltou.
    """
    raw = tc.get("arguments", "")
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        return None, {
            "error": "Invalid JSON in tool arguments",
            "detail": str(e),
            "raw_preview": (raw or "")[:200],
        }
    if not isinstance(args, dict):
        return None, {
            "error": "Tool arguments must be a JSON object",
            "got_type": type(args).__name__,
        }

    if tool_def is None:
        return args, None

    schema = getattr(tool_def, "parameters", None) or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [k for k in required if k not in args]
    if missing:
        return None, {
            "error": "Tool args missing required fields",
            "missing": missing,
            "required": list(required),
        }

    # Filtrar campos fora do schema — chamada com `**args` faria TypeError
    # se o tool nao declara **kwargs no executor.
    if properties:
        args = {k: v for k, v in args.items() if k in properties}

    return args, None


# ── Shared tool-call validation (parallel + sequential) ──
# #D004: extrai o loop de validacao per-tc (~30 linhas) duplicado entre
# `execute_tool_calls` e `_execute_sequential`. Ambas fazem: lookup →
# parse args → enforce workspace → extract safety. Centralizado aqui.

class ValidatedCall(NamedTuple):
    """Resultado de `_validate_tool_call` (#DM079: campos nomeados em vez de
    tupla posicional de 6 elementos cuja ordem só vivia na docstring).

    `error_result is None` → validação passou, prosseguir para execução
    (`args` já com workspace enforcement aplicado). Caso contrário o caller
    emite os eventos de erro com `error_result`/`error_category`.
    """

    error_result: dict | None
    tool_name: str
    args: dict
    safety_str: str
    error_category: str
    tool_def: object | None


def _validate_tool_call(
    tc: dict,
    get_tool_fn: Callable | None,
    workspace: str | None,
) -> ValidatedCall:
    """Validate one tool call. Returns a `ValidatedCall` (NamedTuple).

    If `error_result` is not None: validation failed — caller yields error events.
    Otherwise: args carry workspace enforcement and the call may execute.
    """
    tool_name = _tc_name(tc)
    tool_def = get_tool_fn(tool_name) if get_tool_fn else None

    if tool_def is None:
        return ValidatedCall(
            {"error": f"Ferramenta desconhecida: {tool_name}"},
            tool_name, {}, "unknown", "unknown_tool", None,
        )

    args, parse_error = _parse_and_validate_args(tc, tool_def)
    if parse_error is not None:
        return ValidatedCall(parse_error, tool_name, {}, "denied", "parse_error", tool_def)

    ok, new_args, err = _enforce_workspace(workspace, tool_name, args)
    if not ok:
        return ValidatedCall(
            {"error": err, "workspace_violation": True},
            tool_name, {}, "denied", "violation", tool_def,
        )

    safety = getattr(tool_def, "safety", None)
    safety_str = safety.value if hasattr(safety, "value") else "safe"
    return ValidatedCall(None, tool_name, new_args, safety_str, "", tool_def)


# ── Shared per-call gate (parallel + sequential) ── #DM064: a sequência
# deny-rule → approval prompt → record_approval vivia duplicada nos dois
# modos de dispatch e já driftou uma vez (AUDIT_V1.8 #002: `record_approval`
# ausente no path sequencial enquanto o paralelo — `_execute_tool_calls_inner`
# — já chamava). Centralizada aqui: qualquer mudança no gate vale para os
# dois modos; só a estratégia de concorrência da execução (gather × loop)
# fica em cada caller.

def _invalid_call_events(tc: dict, vc: ValidatedCall, messages: list[dict]):
    """Events (tool_call + tool_result) for a call that failed validation.

    Sync generator so the tool message is appended only AFTER both events
    were consumed — same ordering as the previous inline code.
    """
    error = _annotate_error(vc.error_result, vc.error_category)
    yield {"type": "tool_call", "id": _tc_id(tc), "name": vc.tool_name, "args": {}, "safety": vc.safety_str}
    yield {"type": "tool_result", "id": _tc_id(tc), "name": vc.tool_name, "result": error}
    _append_tool_msg(messages, _tc_id(tc), error, vc.tool_name)


async def _gate_one(
    tc: dict,
    tool_name: str,
    args: dict,
    messages: list[dict],
    needs_approval_fn: Callable[[str, dict], bool],
    approval_callback: Callable[[str, dict], bool] | None,
    is_denied_fn: Callable[[str, dict], tuple[bool, str]],
) -> AsyncGenerator[dict, None]:
    """Deny/approval gate for ONE tool call, shared by both dispatch modes.

    Yields the gate events in the original order (`approval_needed` BEFORE
    the blocking approval callback; skip results after). Contract with the
    caller: an event carrying `denied=True` (built by `_record_skip`) means
    the call was barred — the caller must NOT execute it.
    """
    denied, reason = is_denied_fn(tool_name, args)
    if denied:
        result = {"skipped": True, "reason": reason, "denied_by_rule": True}
        yield _record_skip(tc, tool_name, result, messages)
        return

    if needs_approval_fn(tool_name, args):
        yield {"type": "approval_needed", "name": tool_name, "args": args}
        # Time the user takes to answer is not agent work: credit it back
        # to the turn budget (a prompt left open for hours used to expire
        # the turn the instant it was approved).
        from ._turn_budget import note_user_wait
        _asked_at = time.monotonic()
        try:
            user_approved = (
                await asyncio.to_thread(approval_callback, tool_name, args)
                if approval_callback else False
            )
        finally:
            note_user_wait(time.monotonic() - _asked_at)
        record_approval(user_approved)
        if not user_approved:
            result = {"skipped": True, "reason": "User denied this action"}
            yield _record_skip(tc, tool_name, result, messages)


def _hook_block_skip(tc: dict, tool_name: str, outcome, messages: list[dict]) -> dict:
    """Skip event for a pre_tool hook veto — same shape in both modes."""
    result = {
        "skipped": True,
        "reason": f"Hook blocked: {outcome.block_reason}",
        "hook_blocked": True,
    }
    return _record_skip(tc, tool_name, result, messages)


async def execute_tool_calls(
    tool_calls: list[dict],
    messages: list[dict],
    needs_approval_fn: Callable[[str, dict], bool],
    approval_callback: Callable[[str, dict], bool] | None = None,
    get_tool_fn: Callable | None = None,
    workspace: str | None = None,
    is_denied_fn: Callable[[str, dict], tuple[bool, str]] | None = None,
    provider: str | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Execute tool calls, yielding events for each step.

    `provider` (active LLM provider name) gates vision attachment of
    tool-produced images — omitted/None keeps results text-only.

    When multiple tools are called, auto-approved tools run in PARALLEL.
    Approval-requiring tools are handled sequentially first.

    Yields:
        {"type": "tool_call", ...}
        {"type": "approval_needed", ...}
        {"type": "tool_result", ...}
    """
    is_denied_fn = is_denied_fn or _no_deny

    # #015: publish the active workspace so composite tools' inner _run_tool
    # calls can re-enforce it on sub-tool args (those bypass this gate). Reset
    # in finally so the ContextVar doesn't leak across dispatches.
    from .agents.workspace import active_workspace as _active_ws
    # Fork-with-context: expose the conversation so `delegate_task(
    # inherit_context=true)` can flatten a transcript for the sub-agent.
    # Same set/reset discipline as the workspace var.
    from .tools._delegate_core import (
        parent_messages as _parent_msgs,
        parent_provider as _parent_prov,
    )
    _ws_token = _active_ws.set(workspace)
    _pm_token = _parent_msgs.set(messages)
    _pp_token = _parent_prov.set(provider)
    try:
        async for event in _execute_tool_calls_inner(
            tool_calls, messages, needs_approval_fn, approval_callback,
            get_tool_fn, workspace, is_denied_fn, provider,
        ):
            yield event
    finally:
        _parent_prov.reset(_pp_token)
        _parent_msgs.reset(_pm_token)
        _active_ws.reset(_ws_token)


async def _execute_tool_calls_inner(
    tool_calls: list[dict],
    messages: list[dict],
    needs_approval_fn,
    approval_callback,
    get_tool_fn,
    workspace,
    is_denied_fn,
    provider,
) -> AsyncGenerator[dict, None]:
    if len(tool_calls) == 1:
        async for event in _execute_sequential(
            tool_calls, messages, needs_approval_fn, approval_callback, get_tool_fn,
            workspace=workspace, is_denied_fn=is_denied_fn, provider=provider,
        ):
            yield event
        return

    prepared = []  # (tc, ValidatedCall)

    for tc in tool_calls:
        vc = _validate_tool_call(tc, get_tool_fn, workspace)
        if vc.error_result is not None:
            for event in _invalid_call_events(tc, vc, messages):
                yield event
            continue

        prepared.append((tc, vc))

    for tc, vc in prepared:
        yield {"type": "tool_call", "id": _tc_id(tc), "name": vc.tool_name, "args": vc.args, "safety": vc.safety_str}

    approved = []
    for tc, vc in prepared:
        allowed = True
        async for event in _gate_one(
            tc, vc.tool_name, vc.args, messages,
            needs_approval_fn, approval_callback, is_denied_fn,
        ):
            yield event
            if event.get("denied"):
                allowed = False
        if allowed:
            approved.append((tc, vc.tool_name, vc.args, vc.tool_def))

    if not approved:
        return

    # pre_tool hooks fire in parallel — they're independent shell processes.
    outcomes = await asyncio.gather(
        *[_fire_pre_tool(name, args, workspace) for _, name, args, _ in approved],
        return_exceptions=True
    )
    runnable = []
    for (tc, tool_name, args, tool_def), outcome in zip(approved, outcomes):
        if isinstance(outcome, Exception):
            logger.exception(
                f"Pre-tool hook failed ({tool_name}): {type(outcome).__name__}: {outcome}"
            )
            # Treat as not-blocked — let the tool run, hooks are advisory.
            runnable.append((tc, tool_name, args, tool_def))
            continue
        if outcome.blocked:
            yield _hook_block_skip(tc, tool_name, outcome, messages)
            continue
        runnable.append((tc, tool_name, args, tool_def))

    if not runnable:
        return

    async def _run(item):
        tc, tool_name, args, tool_def = item
        result = await _execute_single_tool(tool_def, tool_name, args)
        return tc, tool_name, args, result

    results = await asyncio.gather(*[_run(item) for item in runnable], return_exceptions=True)

    post_tasks = []
    pending_images: list[tuple[str, list[str]]] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            tc, tool_name, args, _ = runnable[i]
            logger.exception("Parallel tool execution error (%s): %s: %s", tool_name, type(r).__name__, r)
            result = _annotate_error(
                {"error": f"{type(r).__name__}: {r}"}, "runtime"
            )
            yield {"type": "tool_result", "id": _tc_id(tc), "name": tool_name, "result": result}
            _append_tool_msg(messages, _tc_id(tc), result, tool_name)
            continue

        tc, tool_name, args, result = r
        post_tasks.append(_fire_post_tool(tool_name, args, result, workspace))
        yield {"type": "tool_result", "id": _tc_id(tc), "name": tool_name, "result": result}
        _record_result(tc, tool_name, result, messages)
        images = _collect_result_images(result)
        if images:
            pending_images.append((tool_name, images))

    # Vision attachments go after ALL tool messages of the batch — see
    # _append_vision_attachments for why interleaving would break providers.
    if pending_images:
        await asyncio.to_thread(_append_vision_attachments, messages, pending_images, provider)

    if post_tasks:
        # #D120: inspect the gathered results and log any hook failure,
        # mirroring the pre_tool path (which logs). Discarding them silently
        # left a misconfigured post_tool hook failing with no log entry —
        # invisible and hard to diagnose. `_run_hook` already swallows
        # subprocess errors, so this mainly surfaces unexpected ones.
        post_outcomes = await asyncio.gather(*post_tasks, return_exceptions=True)
        for outcome in post_outcomes:
            if isinstance(outcome, Exception):
                logger.exception(
                    "post_tool hook failed: %s: %s",
                    type(outcome).__name__, outcome,
                )


async def _execute_sequential(
    tool_calls: list[dict],
    messages: list[dict],
    needs_approval_fn: Callable[[str, dict], bool],
    approval_callback: Callable[[str, dict], bool] | None = None,
    get_tool_fn: Callable | None = None,
    workspace: str | None = None,
    is_denied_fn: Callable[[str, dict], tuple[bool, str]] = _no_deny,
    provider: str | None = None,
) -> AsyncGenerator[dict, None]:
    pending_images: list[tuple[str, list[str]]] = []
    for tc in tool_calls:
        vc = _validate_tool_call(tc, get_tool_fn, workspace)
        if vc.error_result is not None:
            for event in _invalid_call_events(tc, vc, messages):
                yield event
            continue

        tool_name, args, tool_def = vc.tool_name, vc.args, vc.tool_def
        yield {"type": "tool_call", "id": _tc_id(tc), "name": tool_name, "args": args, "safety": vc.safety_str}

        # AUDIT_V1.8 #002 (histórico): `record_approval` faltava aqui enquanto
        # o path paralelo (`_execute_tool_calls_inner`) já chamava. #DM064:
        # o gate agora é compartilhado em `_gate_one` — os dois modos não
        # podem mais driftar.
        allowed = True
        async for event in _gate_one(
            tc, tool_name, args, messages,
            needs_approval_fn, approval_callback, is_denied_fn,
        ):
            yield event
            if event.get("denied"):
                allowed = False
        if not allowed:
            continue

        outcome = await _fire_pre_tool(tool_name, args, workspace)
        if outcome.blocked:
            yield _hook_block_skip(tc, tool_name, outcome, messages)
            continue

        result = await _execute_single_tool(tool_def, tool_name, args)
        await _fire_post_tool(tool_name, args, result, workspace)
        yield {"type": "tool_result", "id": _tc_id(tc), "name": tool_name, "result": result}
        _record_result(tc, tool_name, result, messages)
        images = _collect_result_images(result)
        if images:
            pending_images.append((tool_name, images))

    # After the loop: a vision message between tool responses of the same
    # assistant batch would 400 on OpenAI-format providers.
    if pending_images:
        await asyncio.to_thread(_append_vision_attachments, messages, pending_images, provider)
