---
name: mvp-2-report
description: Gera o documento MVP_PLAN compilando resultados do mvp-check. Inclui backlog por fases, checkboxes, estimativas. Salva em docs/mvp/current/, move anterior para archive, atualiza STATUS.md. Respeita o escopo definido no mvp-check (global ALL ou camada especifica em monorepos). Use DEPOIS do mvp-check. Trigger quando o usuario mencionar "mvp report", "mvp-report", "gerar mvp plan", "gerar plano mvp", "compilar mvp", "documento mvp", ou qualquer variacao.
---

# MVP Report — Geracao do MVP_PLAN

Compila resultados do mvp-1-check em documento acionavel com backlog priorizado. Respeita o escopo definido durante o check.

## Pre-requisitos

Verifique `docs/mvp/temp/`:
- `mvp_scope.txt` — escopo da analise (`ALL` ou nome do subprojeto)
- `mvp_version.txt` — versao
- `08_summary.md` + arquivos 01-07 das dimensoes

Se algum estiver ausente: "Rode `mvp-1-check` primeiro."

## Etapa 1 — Resolver naming por escopo

Le `mvp_scope.txt`:

| Escopo | Arquivo no current/ | Padrao no archive/ | Sufixo no header |
|--------|---------------------|---------------------|-------------------|
| `ALL` | `MVP_PLAN.md` | `MVP_PLAN_V<X.Y>.md` | `V<X.Y>` |
| `<sub>` | `MVP_PLAN_<SUB_UPPER>.md` | `MVP_PLAN_<SUB_UPPER>_V<X.Y>.md` | `V<X.Y>-<SUB_UPPER>` |

Exemplos:
- `ALL` v3.0 → `current/MVP_PLAN.md`, header `V3.0`
- `naviera-app` v1.0 → `current/MVP_PLAN_NAVIERA_APP.md`, header `V1.0-NAVIERA_APP`
- `api` v2.0 → `current/MVP_PLAN_API.md`, header `V2.0-API`

> **Regra crucial:** escopo focado **NAO arquiva o MVP_PLAN.md global** nem outros MVP_PLAN focados. So arquiva sua propria linhagem (mesmo sufixo). E vice-versa: ALL nao arquiva os focados.

## Etapa 2 — Processo

1. Leia versao, escopo e todos os arquivos temp.
2. **Se existe** o arquivo da mesma linhagem em `docs/mvp/current/`: mova para `docs/mvp/archive/<MESMO_PADRAO>_V<versao_antiga>.md`.
3. **Gere novo** em `docs/mvp/current/<arquivo_resolvido>`. Sem versao no nome — versao no header.
4. **Atualize** `docs/STATUS.md`:
   - Para `ALL`: atualize/insira a linha "MVP Plan" na tabela de auditorias e nos links rapidos.
   - Para escopo focado: insira/atualize linha "MVP Plan (<scope>)" sem remover/sobrescrever a linha "MVP Plan" geral.
5. **Limpe** `docs/mvp/temp/` (apague todos os arquivos da analise — incluindo `mvp_scope.txt`, `mvp_version.txt` e `01-08`).

## Template

```markdown
# MVP PLAN — [NOME_DO_PROJETO][ — <camada>, se focado]
> **Versao:** V[X.Y][-<SCOPE_UPPER>, se focado]
> **Data:** [YYYY-MM-DD]
> **Status:** [PRONTO | QUASE PRONTO | PRECISA DE TRABALHO]
> **Escopo:** [ALL — projeto inteiro] OU [SOMENTE <pasta>; demais camadas seguem MVP_PLAN principal]

---

## RESUMO

| Status | Itens |
|--------|-------|
| PRONTO | X |
| INCOMPLETO | X |
| FALTANDO | X |
| POS-MVP | X |

**Bloqueadores:** [X itens impedem MVP]
**Estimativa total:** [X horas/dias]

---

## FUNCIONALIDADES CORE

### [Nome da feature]
- **Status:** [PRONTO | INCOMPLETO | FALTANDO]
- **Estado atual:** [o que funciona]
- **O que falta:** [se aplicavel]
- **Observacoes:**
> _[espaco]_

---

## FLUXOS CRITICOS

### Fluxo: [Nome]
- **Status:** [PRONTO | INCOMPLETO | FALTANDO]
- **Etapas:**
  - [ ] Etapa 1 — [descricao] — [status]
  - [ ] Etapa 2 — [descricao] — [status]
- **Gaps:** [o que falta]
- **Observacoes:**
> _[espaco]_

---

## INFRAESTRUTURA

| Item | Status | Detalhe |
|------|--------|---------|

- **Observacoes:**
> _[espaco]_

---

## SEGURANCA MINIMA

| Item | Status | Detalhe |
|------|--------|---------|

---

## ESTABILIDADE

| Item | Status | Detalhe |
|------|--------|---------|

---

## UX MINIMA

| Item | Status | Detalhe |
|------|--------|---------|

---

## DEPENDENCIAS

| Servico/API | Status | Configurado | Fallback |
|------------|--------|------------|----------|

---

## PLANO DE ACAO POR FASES

### Fase 1 — Bloqueadores (AGORA)
- [ ] [item] — **Arquivo:** `caminho` — **Esforco:** [estimativa]
- **Notas:**
> _[espaco]_
- **Esforco total:** [X h/d]

### Fase 2 — Incompletos Criticos (esta semana)
- [ ] [item] — **Arquivo:** `caminho` — **Esforco:** [estimativa]
- **Notas:**
> _[espaco]_

### Fase 3 — Estabilidade (antes do lancamento)
- [ ] [item] — **Arquivo:** `caminho` — **Esforco:** [estimativa]
- **Notas:**
> _[espaco]_

### Fase 4 — Polish (pos-lancamento)
- [ ] [item] — **Esforco:** [estimativa]

### Backlog — Pos-MVP
- [ ] [item] — **Prioridade:** [alta/media/baixa]
- **Notas:**
> _[espaco]_

---

## HISTORICO

| Versao | Data | Prontos | Incompletos | Faltando | Status |
|--------|------|---------|-------------|----------|--------|

---

## NOTAS GERAIS
> _[trade-offs, dividas tecnicas, riscos]_

---
*Gerado por Alpha (mvp-2-report) — Revisao humana obrigatoria*
```

## Atualizacao do STATUS.md

### Para escopo `ALL`
- Encontre a linha "MVP Plan" na tabela de auditorias e atualize versao/data/status.
- Atualize o link rapido "MVP Plan".

### Para escopo focado
- **NAO altere** as linhas existentes de "MVP Plan" (geral) ou de outros escopos focados.
- Adicione/atualize uma linha dedicada: `| MVP Plan (<scope>) | V<X.Y>-<SCOPE> | <data> | <bloqueadores> | <status> | [MVP_PLAN_<SCOPE_UPPER>](mvp/current/<arquivo>) |`
- Adicione/atualize um link rapido dedicado: `- **MVP Plan (<scope>):** [MVP_PLAN_<SCOPE_UPPER>](mvp/current/<arquivo>) — V<X.Y>-<SCOPE>, <status curto>`

## Regras

- NAO adicione features novas
- CADA item tem checkbox + observacoes
- Items PRONTO nao entram no plano de acao
- Se genuinamente pronto: "Projeto pronto para lancamento como MVP."
- Em escopo focado, mencione no rodape qual escopo foi analisado e que demais camadas estao em outros MVP_PLANs.
- NUNCA arquive ou apague MVP_PLANs de outros escopos.
