"""Plan mode: sessão em modo somente-leitura até o plano ser aprovado.

Espelha o padrão do Claude Code: o agente pode ler, buscar e analisar,
mas toda tool DESTRUCTIVE é bloqueada até o usuário aprovar um
`present_plan`. Enforcement em três camadas (defesa em profundidade):

1. Filtro das tools oferecidas ao modelo (`filter_openai_tools`) — o
   modelo nem vê write/edit/shell no schema.
2. Hard-block em `approval.is_denied` — se o modelo alucinar o nome de
   uma tool destrutiva fora do schema, o executor nega antes de rodar
   (cobre também os defaults auto-aprovados como write_file).
3. Instrução explícita no turno (`PLAN_MODE_NOTE`) prefixada à mensagem
   do usuário enquanto o modo está ativo.

Saída do modo: aprovação do `present_plan` (via approval_callback) ou
`/plan off` / ciclo Shift+Tab. Estado é in-memory, por sessão — mesmo
contrato do auto-accept (`display.renderers.prompts._approve_all`).
"""

from __future__ import annotations

_active: bool = False

# Tools DESTRUCTIVE que continuam disponíveis em plan mode: o gate delas
# É a aprovação do plano. pre_flight fica de fora — não faz sentido
# planejar batch destrutivo num modo que bloqueia tools destrutivas.
ALLOWED_DESTRUCTIVE = frozenset({"present_plan"})

# AUDIT_V2.3 #207: tools classificadas SAFE mas com efeito colateral
# persistente ou de custo — `record_memory` GRAVA arquivo em disco,
# `google_antigravity` faz chamada LLM externa PAGA. O contrato do plan
# mode é "apenas leia, pesquise e analise"; estas são tratadas como
# não-SAFE nas duas camadas de enforcement abaixo.
SIDE_EFFECT_SAFE_BLOCK = frozenset({"record_memory", "google_antigravity"})

PLAN_MODE_NOTE = (
    "[PLAN MODE ATIVO] Você está em modo de planejamento: apenas leia, "
    "pesquise e analise — tools de escrita/execução estão bloqueadas neste "
    "modo. Quando tiver entendido a tarefa, apresente um plano passo a "
    "passo com a tool `present_plan`. A aprovação do usuário encerra o "
    "plan mode e libera a execução."
)


def is_active() -> bool:
    return _active


def set_active(value: bool) -> None:
    global _active
    _active = bool(value)


def toggle() -> bool:
    global _active
    _active = not _active
    return _active


def filter_openai_tools(tools: list[dict], get_tool_fn) -> list[dict]:
    """Mantém só tools SAFE (+ ALLOWED_DESTRUCTIVE) da lista OpenAI-format.

    Tools desconhecidas ao registry (get_tool_fn retorna None) são
    removidas por segurança — em plan mode, o default é fail-closed.
    """
    from .tools import ToolSafety

    out: list[dict] = []
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        name = (fn or {}).get("name") if isinstance(fn, dict) else None
        if not name:
            continue
        if name in ALLOWED_DESTRUCTIVE:
            out.append(t)
            continue
        if name in SIDE_EFFECT_SAFE_BLOCK:  # #207: SAFE com efeito colateral
            continue
        td = get_tool_fn(name)
        if td is not None and td.safety == ToolSafety.SAFE:
            out.append(t)
    return out


def deny_reason(tool_name: str) -> str | None:
    """Motivo de negação para `tool_name` sob plan mode, ou None se permitida.

    Chamada por `approval.is_denied` (camada 2). Tools fora do registry
    são negadas (fail-closed) — em operação normal elas nem despacham,
    mas aqui a negação produz mensagem clara para o modelo.
    """
    if not _active:
        return None
    if tool_name in ALLOWED_DESTRUCTIVE:
        return None
    if tool_name in SIDE_EFFECT_SAFE_BLOCK:
        # #207: SAFE no registry, mas grava disco / gasta dinheiro — fora
        # do contrato read-only do plan mode.
        return (
            "Plan mode ativo: esta tool tem efeito colateral (persistência "
            "em disco ou chamada externa paga) e fica bloqueada até o plano "
            "ser aprovado via `present_plan`."
        )
    from .tools import ToolSafety, get_tool

    td = get_tool(tool_name)
    if td is not None and td.safety == ToolSafety.SAFE:
        return None
    return (
        "Plan mode ativo: tools destrutivas estão bloqueadas. "
        "Apresente um plano com `present_plan` e aguarde a aprovação "
        "do usuário para executar."
    )
