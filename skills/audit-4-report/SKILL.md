---
name: audit-4-report
description: Gera o relatorio final de auditoria compilando todos os resultados. Move versao anterior para archive, salva novo em docs/audits/current/, e atualiza STATUS.md. Use DEPOIS do audit-3-review. Trigger quando o usuario mencionar "audit report", "gerar relatorio", "compilar auditoria", "audit-report", "relatorio final", "gerar AUDIT", ou qualquer variacao.
---

# Audit Report — Geracao do Relatorio Final

Compila resultados das etapas anteriores em relatorio unico, versionado e acionavel.

## Pre-requisito

Verifique em `docs/audits/temp/`:
- `version.txt` e `00_setup.md` — se nao: "Rode `audit-1-setup` primeiro."
- Pelo menos um arquivo 01-07 — se nao: "Rode `audit-2-scan` primeiro."
- `08_review.md` — se nao: avise que e recomendado, pergunte se continua sem.

## Processo (atomico — se algum passo falhar, versao anterior continua intacta em current/)

1. Leia `version.txt` para versao.
2. Leia todos os arquivos 00-08.
3. Aplique correcoes do 08_review (remover falsos positivos, ajustar severidades, substituir fixes, adicionar novos problemas).
4. **GERAR NOVO PRIMEIRO**: salve em `docs/audits/current/AUDIT_V[versao].md`. Como a versao e incrementada pelo audit-1-setup, ela e **diferente** da versao do AUDIT anterior — ambos coexistem brevemente em `current/` sem conflito.
5. **VALIDE**: confira que o arquivo foi escrito com conteudo completo (tem secoes "RESUMO EXECUTIVO" e "PROBLEMAS ENCONTRADOS"). Se falhar, NAO prossiga — o antigo continua em `current/` como fallback.
6. **ARQUIVAR ANTIGO**: depois que o novo esta no lugar e validado, mova o(s) AUDIT_V*.md mais antigo(s) em `current/` (qualquer um com versao diferente da atual) para `docs/audits/archive/`.
7. **ATUALIZAR STATUS.md**: atualize a secao de auditorias e issues criticas em `docs/STATUS.md`.
8. **LIMPAR**: delete `docs/audits/temp/` e todo conteudo (somente apos todos os passos acima terem concluido).

**Se qualquer passo entre 4 e 6 falhar**: NAO delete `temp/`. O usuario pode re-rodar a skill sem perder trabalho.

## Template do relatorio

```markdown
# AUDITORIA DE CODIGO — [NOME_DO_PROJETO]
> **Versao:** V[X.Y]
> **Data:** [YYYY-MM-DD]
> **Auditor:** Alpha (Dev Senior Audit)
> **Stack:** [stack]
> **Escopo:** Auditoria completa

---

## RESUMO EXECUTIVO

| Severidade | Quantidade |
|-----------|-----------|
| CRITICO | X |
| ALTO | X |
| MEDIO | X |
| BAIXO | X |
| **TOTAL** | **X** |

**Status geral:** [REPROVADO PARA PRODUCAO / APROVADO COM RESSALVAS / APROVADO]

Criterio: CRITICO = REPROVADO | Apenas ALTO ou menor = RESSALVAS | Apenas MEDIO/BAIXO = APROVADO

---

## MAPEAMENTO ESTRUTURAL

[Conteudo de 00_setup.md]

---

## PROBLEMAS ENCONTRADOS

### 2.1 — Bugs Criticos e Runtime

#### Issue #001 — [Titulo]
- [ ] **Concluido**
- **Severidade:** CRITICO
- **Arquivo:** `caminho/arquivo`
- **Linha(s):** XX-YY
- **Problema:** [descricao]
- **Impacto:** [impacto]
- **Codigo problematico:**
\`\`\`[lang]
// codigo atual
\`\`\`
- **Fix sugerido:**
\`\`\`[lang]
// codigo corrigido
\`\`\`
- **Observacoes:**
> _[espaco para anotacoes manuais]_

---

[Repetir para cada issue. Agrupar 2.1-2.6. Categoria sem issues = "Nenhum problema encontrado."]

### 2.2 — Seguranca
### 2.3 — Logica de Negocio
### 2.4 — Resiliencia e Error Handling
### 2.5 — Performance
### 2.6 — Manutenibilidade
### 2.7 — Testes

---

## CONTRA-VERIFICACAO

### Falsos positivos descartados
### Severidades ajustadas
### Pontos cegos declarados

---

## PLANO DE CORRECAO

### Sprint 1 — Criticos (fazer AGORA)
- [ ] Issue #XXX — [titulo]
- **Notas:**
> _[espaco]_

### Sprint 2 — Altos (esta semana)
- [ ] Issue #XXX — [titulo]
- **Notas:**
> _[espaco]_

### Sprint 3 — Medios (este mes)
- [ ] Issue #XXX — [titulo]
- **Notas:**
> _[espaco]_

### Backlog — Baixos
- [ ] Issue #XXX — [titulo]
- **Notas:**
> _[espaco]_

---

## HISTORICO DE AUDITORIAS

| Versao | Data | Total | Criticos | Status |
|--------|------|-------|----------|--------|

[Leia audits anteriores em archive/ e current/ para preencher]

---

## NOTAS GERAIS
> _[observacoes, dividas tecnicas, recomendacoes]_

---

## OBRIGATORIO — ATUALIZAR DOCS APOS CONCLUIR TAREFAS

Toda issue listada tem checkbox `- [ ] **Concluido**` (vazio) e aparece na lista sumario do "Plano de Correcao". **Sempre que voce (humano ou IA) corrigir uma issue listada aqui — na MESMA sessao em que aplicou o fix, antes de passar para a proxima:**

1. **Marque o checkbox da issue detalhada**:
   - De: `- [ ] **Concluido**`
   - Para: `- [x] **Concluido** _(corrigido YYYY-MM-DD — commit <hash> ou breve descricao)_`
2. **Marque tambem o checkbox da lista sumario** (Sprint 1/2/3 ou Backlog) referente a essa issue.
3. **Atualize a tabela RESUMO EXECUTIVO** no topo (contadores por severidade).
4. **Atualize `docs/STATUS.md`**: contadores de issues abertas/fechadas e tabela de issues criticas.
5. Em commits, use `fix(<scope>): closes #NNN — descricao` ou inclua a referencia da issue na mensagem.

**Por que e obrigatorio:** doc dessincronizado entre commits e estado real do codigo perde confiabilidade. Proximas sessoes confiam neste arquivo para saber o que falta. Sem isso ha retrabalho ou skip de issues reais — exatamente o que esta auditoria existe para evitar.

NAO acumule atualizacoes para "depois". Faca em cada fix.

---
*Gerado por Alpha — Revisao humana obrigatoria*
```

## Atualizacao do STATUS.md

Apos gerar o relatorio, atualize `docs/STATUS.md` com:
- Estado geral baseado no resultado do audit
- Tabela de issues criticas abertas com links
- Linha na secao de auditorias com versao, data, contagem
- Link para o relatorio em current/

Se STATUS.md nao existir, crie-o usando o template completo (veja skill status-update para referencia).

## Regras

- NAO invente problemas
- CADA issue tem checkbox + observacoes + codigo real
- Categorias limpas = "Nenhum problema encontrado."
- Issues descartadas na review NAO aparecem no relatorio final
- Numere issues sequencialmente sem reiniciar entre categorias
