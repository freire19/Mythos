# RFC: Pre-flight cards — estratégia antes de execução

**Status:** draft
**Author:** Claude (Opus 4.7) + Jonatas Freire
**Created:** 2026-05-19
**Plano-Upgrade-v3 ref:** novo — fora do escopo dos horizontes H1/H2/H3 atuais

---

## Problema

Hoje o agent loop é **reativo**: a cada tool call, o usuário aprova ou nega.
Não existe **plano explícito** antes de gastar dinheiro/tempo. Consequências:

1. **Cost surprise.** `/cost` mostra o gasto **depois**. Loops longos queimam
   $$ sem sinal. Não há budget cap por turno.
2. **Aprovação fadiga.** Aprovar 12 tools individualmente em sequência (cada
   um sem contexto do todo) é pior que aprovar 1 estratégia coerente.
3. **Sem dado pra melhorar estimativa.** O agente nunca registra "eu disse
   que isso ia gastar X, gastou Y" — sem feedback loop, estimativa nunca
   melhora.
4. **Estratégia é implícita.** O usuário só descobre o que o agente está
   tentando fazer **observando a sequência de tools**. `present_plan`
   ajuda mas é prosa pra humano, não estruturado e sem custo quantificado.

Outros frameworks (Aider, Codex, Claude Code, Cursor) também não resolvem
isso bem — todos revelam ações conforme acontecem. **Esta seria a primeira
implementação séria de pre-flight num agente CLI.**

---

## Proposta

Antes de executar um batch destrutivo de tools, o agente é obrigado a
emitir um **pre-flight card** estruturado contendo:

- **Objetivo** do turno (1 frase)
- **Tools planejadas** com custo estimado por tool
- **Custo total estimado** (USD, baseado em token estimate × provider rate)
- **Tempo total estimado** (baseado em latência histórica por tool)
- **Alternativas rejeitadas** com motivo
- **Confiança** (high/medium/low) na estratégia

Usuário responde:
- **approve** — executa como planejado
- **modify** — abre editor pra editar a lista de tools (mata os tools removidos)
- **reject** — cancela o turno, retorna ao prompt
- **skip-similar** — auto-aprova cards futuros com mesmo padrão (`goal` semelhante + custo abaixo de N% do approvado)

### Anatomia do card (UI)

```
┌─ Pre-flight (turn 3) ──────────────────────────────────────┐
│ Goal: Localizar e corrigir o memory leak em executor.py    │
│                                                            │
│ Planned (5 tools, ~$0.08, ~12s):                           │
│   1. grep "executor" alpha/                  $0.001  0.3s  │
│   2. read_file alpha/executor.py             $0.002  0.4s  │
│   3. execute_python (profile script)         $0.04   8s    │
│   4. edit_file alpha/executor.py             $0.02   2s    │
│   5. pytest tests/test_executor.py           $0.01   2s    │
│                                                            │
│ Alternatives rejected:                                     │
│   ✗ tracemalloc deep dive (would cost ~$0.40)              │
│   ✗ rewrite executor.py from scratch (low confidence)      │
│                                                            │
│ Confidence: medium (leak suspected line ~150)              │
│                                                            │
│ [ approve · modify · reject · skip-similar ]               │
└────────────────────────────────────────────────────────────┘
```

---

## Wire format

Nova tool registrada em `alpha/tools/plan_tools.py`:

```python
register_tool(ToolDefinition(
    name="pre_flight",
    description=(
        "Emit a structured plan card before executing a batch of tools. "
        "REQUIRED at the start of any turn that will call 2+ DESTRUCTIVE "
        "tools OR is expected to cost more than $0.05. The user reviews "
        "the card and approves the whole strategy at once instead of "
        "approving each tool individually."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "One sentence describing what this turn accomplishes."
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "args_preview": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["tool", "args_preview"],
                },
            },
            "alternatives_rejected": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "approach": {"type": "string"},
                        "why_rejected": {"type": "string"},
                    },
                },
                "default": [],
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
        },
        "required": ["goal", "steps", "confidence"],
    },
    safety=ToolSafety.DESTRUCTIVE,  # forces approval gate, like present_plan
    category=ToolCategory.PLANNING,
    executor=_pre_flight,
))
```

`_pre_flight` retorna o dict enriquecido com estimativas (custo + tempo)
calculadas pelos componentes abaixo. O renderer no `display/` reage ao
shape `{type: "pre_flight_card", ...}` (semelhante a como `plan_card`
funciona hoje).

### System prompt rule

Adicionar a `prompts/system.md`:

```
Before executing 2+ destructive tools in the same turn, OR before any
single tool expected to cost more than $0.05, you MUST first call
`pre_flight(goal, steps, alternatives_rejected, confidence)`. The user
reviews and approves the strategy as a whole. After approval the steps
execute without individual approval prompts (pre-flight IS the approval).

Skip pre_flight when: read-only exploration (grep, ls, read_file in
isolation), or single-tool turns that are obviously cheap.
```

---

## Componentes a construir

| Componente | Arquivo | Esforço |
|---|---|---|
| Tool definition + executor | `alpha/tools/plan_tools.py` (estende) | 1h |
| Cost estimator (tokens × rate) | `alpha/preflight/cost_estimate.py` (novo) | 2h |
| Time estimator (lookup latência histórica) | `alpha/preflight/time_estimate.py` (novo) | 1h |
| Card renderer | `alpha/display/renderers/preflight.py` (novo) | 1h |
| Budget cap (env var + interrupt) | `alpha/agent/__init__.py` (modifica) | 1h |
| Feedback capture (approve/modify/reject → memory) | `alpha/memory.py` (estende) | 2h |
| System prompt update | `alpha/prompts/system.md` (modifica) | 30min |
| Tests | `tests/test_preflight.py` (novo) | 3h |
| **Total** | — | **~11h** |

---

## Budget caps

Nova env var:

- `ALPHA_MAX_TURN_COST_USD=0.50` — interrompe turn antes do prompt sair
  se o pre-flight estimar acima.
- `ALPHA_MAX_SESSION_COST_USD=5.00` — interrompe a sessão inteira ao
  atingir (acumulado, lido de `~/.alpha/cost/<session-id>.jsonl`).

Default: ambos `unset` = sem limite (preserva comportamento atual).

Quando o limite é atingido, o REPL mostra:

```
Pre-flight aborted: estimated $0.62, exceeds ALPHA_MAX_TURN_COST_USD=0.50.
Options: [increase cap · split task · approve anyway · cancel]
```

---

## Feedback loop (resolve dor #2 — agente não aprende)

Cada interação com o card vira sinal em `~/.alpha/memory/preflight_feedback.jsonl`:

```json
{"timestamp": "...", "session": "...", "goal": "...",
 "estimated_cost": 0.08, "actual_cost": 0.11, "decision": "approve"}

{"timestamp": "...", "session": "...", "goal": "...",
 "estimated_cost": 0.40, "decision": "modify",
 "removed_steps": ["execute_python"]}
```

Próxima feature (out of scope deste RFC mas habilitada): o agente lê esse
histórico e ajusta confidence + estimativas em cards futuros para tipos
de objetivo semelhantes.

---

## Migration / backwards compat

- `pre_flight` é nova tool. Sistema prompt obriga o uso. Sessões antigas
  sem isso continuam funcionando — apenas não emitem cards.
- `present_plan` existente fica como está. É narrativo pra humano. Pre-flight
  é estruturado pra controle. Coexistem; o agente pode chamar os dois
  (`present_plan` para tarefa grande, `pre_flight` por turn).
- Opt-out: `ALPHA_DISABLE_PREFLIGHT=1` desliga a obrigação no system prompt
  via injeção condicional. Útil pra power users e CI.

---

## Open questions (precisam decisão antes da implementação)

1. **Card é DESTRUCTIVE (precisa aprovação) ou SAFE (auto-aprovado, mas
   ainda exibido)?** Se DESTRUCTIVE, o gate de aprovação atual já cobre.
   Se SAFE, precisamos de UI custom que aceita os 4 botões. **Sugestão:**
   DESTRUCTIVE, reaproveita máquina existente.

2. **Modify edita JSON ou abre editor visual?** JSON é mais simples (1
   linha pra remover step). Editor visual (prompt_toolkit form) é mais
   amigável mas 4h de trabalho a mais. **Sugestão:** JSON na primeira
   versão, editor visual em fase 2 se houver demanda.

3. **`skip-similar` é por sessão ou persiste em disco?** Persistir cria
   risco de "auto-approval" indesejado em sessão futura. **Sugestão:**
   por sessão apenas, sem persistência.

4. **O que fazer se o agente NÃO chamar pre_flight quando devia?**
   Opções: (a) silenciar e deixar passar, (b) injetar `[SYSTEM NOTE]
   pre_flight required for this turn` e re-prompt, (c) auto-gerar
   pre-flight a partir das tool calls planejadas. **Sugestão:** (b)
   re-prompt na primeira ofensa, (c) fallback se reprompt falha.

5. **Cost estimator: tokens reais ou heurística?** Real exige passar os
   messages pelo tokenizer do provider (custa CPU). Heurística (chars/4)
   é instantâneo mas impreciso. **Sugestão:** heurística com erro
   marcado, real só se `ALPHA_ACCURATE_COST_ESTIMATE=1`.

---

## Out of scope (separar em RFCs futuros)

- **Auto-learning de cost estimate baseado em histórico.** Habilitado por
  este RFC mas implementação separada.
- **Group multi-turn into one "campaign" with shared budget.** Útil mas
  exige máquina nova de tracking; este RFC é por turn.
- **Pre-flight para sub-agentes.** Sub-agentes herdam do parent — não
  faz pre-flight próprio para evitar UX nested. Decisão revisitável.

---

## Métricas de sucesso

Como saber se vale o trabalho:
- Usuários ativam `ALPHA_MAX_TURN_COST_USD`? (sinal de que budget cap
  preenche dor real)
- Decisão modify > approve em >20% dos cards? (sinal de que o agente
  consistentemente over-plans e o ajuste manual é valioso)
- Custo médio por turn cai entre semana N e semana N+4 do uso? (sinal
  de que o feedback loop ajusta estimativas)
