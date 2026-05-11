---
name: audit-deep
description: Faz auditoria profunda e especializada em UMA categoria do codigo, usando o ultimo AUDIT como base. Vai muito alem do audit-2-scan — verifica cada arquivo linha por linha. Categorias disponiveis — security, logic, resilience, performance, maintainability, bugs. Use DEPOIS de ter pelo menos um AUDIT gerado. Trigger quando o usuario mencionar "audit deep", "auditoria profunda", "deep dive", "audit-deep", "aprofundar auditoria", "verificacao profunda de seguranca/logica/performance/etc", ou qualquer variacao.
---

# Audit Deep — Auditoria Profunda por Categoria

Mergulho profundo em UMA categoria. Enquanto audit-2-scan faz cobertura ampla (6 categorias, profundidade media), audit-deep dedica toda a sessao a UMA (cobertura total, profundidade maxima).

## Etapa 1 — Identificar categoria e contexto

1. Identifique a categoria. Se nao especificada, **chame a tool `ask_choice`** com:
   - `question`: "Qual categoria você quer auditar profundamente?"
   - `options`: lista exatamente nesta ordem —
     - `security — Vulnerabilidades, secrets, injection, auth`
     - `logic — Erros de lógica, edge cases, race conditions`
     - `resilience — Tratamento de erros, timeouts, retry, graceful degradation`
     - `performance — Gargalos, uso de memória, I/O blocking, async`
     - `maintainability — Código morto, duplicação, acoplamento, nomes`
     - `bugs — Bugs funcionais, comportamento incorreto`

   Use `chosen_value.split(' — ')[0]` para extrair a chave (`security`, `logic`, etc).
   **Nunca** renderize a lista como tabela markdown — vem bagunçada no terminal.

2. Encontre o AUDIT mais recente em `docs/audits/current/`. Se nao existir, tente `docs/`, `/audits/`. Nenhum: "Rode audit-1-setup + audit-2-scan + audit-4-report primeiro." e pare.

3. Leia o AUDIT. Extraia: mapeamento estrutural, issues da categoria, pontos cegos.

4. Leia o reference file: `references/[categoria].md` — contem o checklist especializado.

## Etapa 2 — Auditoria profunda

Siga o checklist do reference file. Diferenca do scan:
- **audit-2-scan**: padroes obvios nos arquivos principais
- **audit-deep**: CADA arquivo, linha por linha, CADA item do checklist

Para cada arquivo relevante: leia inteiro, aplique TODOS os items do checklist.

## Etapa 3 — Verificar issues anteriores

Para cada issue da categoria no AUDIT anterior:
- Fix aplicado? → RESOLVIDO
- Fix parcial? → documente o que falta
- Nao aplicado? → PENDENTE

## Etapa 4 — Salvar resultado

1. **Se existe** `DEEP_[CATEGORIA].md` em `docs/audits/current/`: leia a versao interna, mova para `docs/audits/archive/DEEP_[CATEGORIA]_V[versao_antiga].md`
2. **Gere novo** em `docs/audits/current/DEEP_[CATEGORIA].md` (sem versao no nome do arquivo, versao fica no header)

```markdown
# AUDITORIA PROFUNDA — [CATEGORIA] — [NOME_DO_PROJETO]
> **Versao:** V[X.Y]
> **Data:** [YYYY-MM-DD]
> **Categoria:** [nome]
> **Base:** AUDIT_V[versao do audit base]
> **Arquivos analisados:** [X de Y total]

---

## RESUMO

| Status | Quantidade |
|--------|-----------|
| Novos problemas | X |
| Issues anteriores resolvidas | X |
| Issues anteriores parcialmente resolvidas | X |
| Issues anteriores pendentes | X |
| **Total de issues ativas** | **X** |

---

## ISSUES ANTERIORES — STATUS

### Resolvidas
| Issue | Titulo | Verificacao |
|-------|--------|------------|

### Parcialmente resolvidas
| Issue | Titulo | O que falta |
|-------|--------|------------|

### Pendentes
| Issue | Titulo | Observacao |
|-------|--------|-----------|

---

## NOVOS PROBLEMAS

#### Issue #D[NNN] — [Titulo]
- [ ] **Concluido**
- **Severidade:** [CRITICO | ALTO | MEDIO | BAIXO]
- **Arquivo:** `caminho/arquivo`
- **Linha(s):** XX-YY
- **Problema:** [descricao]
- **Impacto:** [impacto]
- **Codigo problematico:**
\`\`\`[lang]
\`\`\`
- **Fix sugerido:**
\`\`\`[lang]
\`\`\`
- **Observacoes:**
> _[espaco]_

---

## COBERTURA

| Arquivo | Analisado | Issues |
|---------|-----------|--------|

---

## PLANO DE CORRECAO

### Urgente (CRITICO + ALTO)
- [ ] Issue #DXXX — [titulo] — **Esforco:** [estimativa]
- **Notas:**
> _[espaco]_

### Importante (MEDIO)
- [ ] Issue #DXXX — [titulo] — **Esforco:** [estimativa]

### Menor (BAIXO)
- [ ] Issue #DXXX — [titulo] — **Esforco:** [estimativa]

---

## NOTAS
> _[observacoes para esta categoria]_

---

## OBRIGATORIO — ATUALIZAR DOCS APOS CONCLUIR TAREFAS

Toda issue tem checkbox `- [ ] **Concluido**` na sua secao detalhada e tambem na lista sumario "Plano de Correcao". **Sempre que voce (humano ou IA) corrigir uma issue desta categoria — na MESMA sessao em que aplicou o fix, antes de passar para a proxima:**

1. **Marque o checkbox da issue detalhada** (secao "NOVOS PROBLEMAS" ou correspondente):
   - De: `- [ ] **Concluido**`
   - Para: `- [x] **Concluido** _(corrigido YYYY-MM-DD — commit <hash> ou breve descricao do fix)_`
2. **Marque tambem o checkbox da lista sumario** ("Plano de Correcao" — Urgente/Importante/Menor).
3. **Mova a issue na tabela "ISSUES ANTERIORES — STATUS"** se for re-verificacao de scan anterior (de Pendentes para Resolvidas, com descricao do fix).
4. **Atualize a tabela RESUMO** no topo (contadores: novos, resolvidas, parciais, pendentes, total ativo).
5. **Atualize `docs/STATUS.md`**: linha do deep audit (contadores por severidade).
6. Em commits, use `fix(<scope>): closes #DXXX — descricao` ou inclua a referencia da issue na mensagem.

**Por que e obrigatorio:** doc dessincronizado entre commits e estado real do codigo perde confiabilidade. Proximas sessoes confiam neste arquivo para saber o que falta. Sem isso, ALTOs/MEDIOs/BAIXOs ja corrigidos continuam aparecendo como abertos e geram retrabalho — exatamente o que esta auditoria profunda existe para evitar.

NAO acumule atualizacoes para "depois". Faca em cada fix, antes do commit.

---
*Gerado por Alpha (Deep Audit) — Revisao humana obrigatoria*
```

3. **Atualize `docs/STATUS.md`**: adicione/atualize linha do deep audit na tabela de auditorias.

## Etapa 5 — Atualizar docs ao corrigir issues

**SEMPRE que corrigir issues (durante o deep ou em qualquer momento):**

1. **AUDIT_V*.md** (em `docs/audits/current/`): marque o checkbox da issue como concluido:
   - De: `- [ ] **Concluido**`
   - Para: `- [x] **Concluido** _(corrigido YYYY-MM-DD)_`

2. **DEEP_[CATEGORIA].md**: mova a issue de "Pendentes" para "Resolvidas", incluindo descricao do fix aplicado.

3. **STATUS.md**: atualize contadores (CRITICAS, ALTAS, etc.) e a tabela de issues abertas.

4. **Resumos numericos**: atualize totais no header do DEEP (pendentes, resolvidas, total ativo).

**NAO deixe para atualizar depois** — faca imediatamente apos cada correcao, antes de passar para a proxima issue.

## Postura

- Va ALEM do scan. Nao repita issues — referencie e verifique se foram corrigidas.
- Leia cada arquivo inteiro, nao apenas trechos flagrados.
- Se tudo resolvido e nada novo: "Categoria [X] limpa apos correcoes."
