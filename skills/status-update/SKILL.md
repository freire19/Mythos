---
name: status-update
description: Recompila o STATUS.md lendo todos os documentos ativos em docs/. Atualiza estado geral, issues criticas, auditorias, sprints e links. Use apos corrigir issues, apos rodar qualquer skill de audit/mvp, ou quando quiser uma visao atualizada do projeto. Trigger quando o usuario mencionar "status update", "atualizar status", "status-update", "atualizar dashboard", "recompilar status", "como ta o projeto", "visao geral do projeto", "resumo do projeto", ou qualquer variacao de atualizar/ver o status geral.
---

# Status Update — Painel de Controle do Projeto

Le todos os documentos ativos em `docs/` e recompila o `STATUS.md` — a porta unica de entrada para entender o estado do projeto.

## Processo

1. Leia o AUDIT mais recente em `docs/audits/current/`
2. Leia os deep audits em `docs/audits/current/` (DEEP_*.md)
3. Leia o MVP_PLAN em `docs/mvp/current/`
4. Leia ADRs em `docs/decisions/`
5. Analise o codigo para verificar quais issues foram corrigidas desde o ultimo doc
6. Compile tudo em `docs/STATUS.md`

## Verificacao de issues corrigidas

Para cada issue CRITICA e ALTA listada nos documentos:
- Leia o arquivo e linha referenciados
- O fix sugerido foi aplicado? O codigo mudou?
- Se corrigido: marque como RESOLVIDO
- Se parcialmente corrigido: marque como EM PROGRESSO
- Se nao corrigido: marque como PENDENTE

Isso e a parte mais valiosa: o STATUS.md reflete o estado REAL, nao apenas o que os docs dizem.

## Template do STATUS.md

```markdown
# STATUS DO PROJETO — [NOME]
> Ultima atualizacao: [YYYY-MM-DD HH:MM]
> Atualizado por: Alpha (status-update)

---

## Estado Geral: [BLOQUEADO | EM PROGRESSO | PRONTO PARA MVP | EM PRODUCAO]

### Resumo
[2-3 frases sobre onde o projeto esta agora. Inclua: quantas issues criticas pendentes, se o MVP esta pronto, o que esta sendo trabalhado.]

---

## ISSUES CRITICAS ABERTAS

| # | Issue | Severidade | Status | Fonte | Arquivo |
|---|-------|-----------|--------|-------|---------|
| #003 | Middleware duplicado | CRITICO | Pendente | [AUDIT](audits/current/AUDIT_V1.2.md) | `apps/web/src/middleware.ts` |
| #006 | Refresh token replay | CRITICO | Em progresso | [AUDIT](audits/current/AUDIT_V1.2.md) | `apps/api/src/routes/auth.ts` |

[Apenas CRITICO e ALTO. Se nenhuma: "Nenhuma issue critica pendente."]

---

## ISSUES RESOLVIDAS RECENTEMENTE

| # | Issue | Resolvido em | Verificado |
|---|-------|-------------|-----------|
| #001 | Console.log vaza dados | 2026-03-26 | Codigo verificado |

---

## AUDITORIAS

| Tipo | Versao | Data | Issues ativas | Status | Doc |
|------|--------|------|--------------|--------|-----|
| Audit Geral | V1.2 | [data] | X | [status] | [Link](audits/current/AUDIT_V1.2.md) |
| Deep Security | V1.0 | [data] | X | [status] | [Link](audits/current/DEEP_SECURITY.md) |
| Deep Logic | — | — | — | Nao realizado | — |
| MVP Plan | V1.0 | [data] | X bloqueadores | [status] | [Link](mvp/current/MVP_PLAN.md) |

---

## SPRINT ATUAL

[Se houver issues marcadas como "Em progresso" nos docs:]

- [ ] Issue #XXX — [titulo] — **Status:** [Pendente | Em progresso | Concluido]
- [ ] Issue #XXX — [titulo] — **Status:** [Pendente | Em progresso | Concluido]

**Progresso:** [X de Y concluidos]

---

## PROXIMO SPRINT (sugerido)

[Proximas issues por prioridade que ainda nao foram iniciadas:]

- [ ] Issue #XXX — [titulo] — **Severidade:** ALTO
- [ ] Issue #XXX — [titulo] — **Severidade:** MEDIO

---

## METRICAS DE PROGRESSO

| Metrica | Valor |
|---------|-------|
| Total de issues encontradas (historico) | X |
| Issues resolvidas | X |
| Issues pendentes | X |
| Taxa de resolucao | X% |
| Issues criticas pendentes | X |
| MVP bloqueadores restantes | X |

---

## DECISOES RECENTES

| Data | Decisao | Doc |
|------|---------|-----|
| [data] | [descricao curta] | [Link](decisions/XXX.md) |

[Se nenhuma ADR existe: "Nenhuma ADR registrada. Considere documentar decisoes arquiteturais importantes."]

---

## LINKS RAPIDOS

- **Audit atual:** [Link](audits/current/AUDIT_V[X.Y].md)
- **MVP Plan:** [Link](mvp/current/MVP_PLAN.md)
- **Deep Security:** [Link](audits/current/DEEP_SECURITY.md) (se existir)
- **Deep Logic:** [Link](audits/current/DEEP_LOGIC.md) (se existir)
- **Deploy runbook:** [Link](runbooks/deploy.md) (se existir)

---

## TIMELINE

| Data | Evento |
|------|--------|
| [data] | Primeiro audit (V1.0) — X issues encontradas |
| [data] | Sprint 1 concluido — X issues resolvidas |
| [data] | Deep security — X novos problemas |
| [data] | MVP Plan gerado — X bloqueadores |

[Construa a timeline lendo datas dos docs em current/ e archive/]

---
*Atualizado automaticamente — Revisao humana recomendada*
```

## Regras

- STATUS.md e o UNICO arquivo que o dev precisa abrir pra entender o projeto
- TODOS os links devem ser relativos e funcionais
- Se um doc referenciado nao existe, nao inclua o link (diga "Nao realizado")
- A verificacao de issues no codigo e essencial — nao copie status do doc sem conferir
- Se nao conseguir verificar todas as issues (projeto grande), declare quais verificou e quais nao
- Mantenha o STATUS.md conciso — e um dashboard, nao um relatorio. Links levam ao detalhe.

## Quando rodar

- Apos qualquer audit-4-report ou mvp-2-report
- Apos um sprint de correcoes
- Quando o dev perguntar "como ta o projeto?"
- Periodicamente (sugira ao usuario rodar semanalmente)
