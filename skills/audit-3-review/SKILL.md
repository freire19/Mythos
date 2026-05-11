---
name: audit-3-review
description: Contra-verifica os resultados de uma auditoria de codigo. Le os arquivos gerados pelo audit-2-scan em docs/audits/temp/, valida cada issue, descarta falsos positivos, ajusta severidades, verifica fixes. Use DEPOIS do audit-2-scan. Trigger quando o usuario mencionar "audit review", "contra-verificar", "revisar auditoria", "audit-review", "validar issues", ou qualquer variacao de revisar/validar uma auditoria.
---

# Audit Review — Contra-Verificacao

Revisa criticamente os resultados do audit-2-scan. Elimina falsos positivos, ajusta severidades, encontra o que passou batido.

## Pre-requisito

Verifique se existem arquivos 01-07 em `docs/audits/temp/`. Se nao: "Rode `audit-2-scan` primeiro." e pare.
Leia tambem `docs/audits/temp/00_setup.md`.

## Etapa 1 — Validacao de cada issue

Para CADA issue nos arquivos 01-07:
1. E realmente um bug ou comportamento intencional?
2. O fix sugerido esta correto? Introduz bug novo?
3. A severidade esta correta?

## Etapa 2 — Busca por problemas nao detectados

Ultima varredura focada em:
- Interacoes entre modulos que o scan perdeu
- Configuracoes de deploy/infra
- Dependencias transitivas

## Etapa 3 — Salvar resultado

Salve em `docs/audits/temp/08_review.md`:

```markdown
# Contra-Verificacao

## Falsos Positivos Descartados
| Issue | Motivo do descarte |
|-------|-------------------|

## Severidades Ajustadas
| Issue | De | Para | Motivo |
|-------|-----|------|--------|

## Fixes Corrigidos
| Issue | Problema no fix original | Fix revisado |
|-------|------------------------|-------------|

## Novos Problemas Encontrados
[mesmo formato do scan, ou "Nenhum"]

## Pontos Cegos Declarados
[areas nao cobertas e por que]
```

## Principios

- Prefira descartar falso positivo a manter issue duvidosa
- Fix errado e pior que nenhum fix — corrija-o
- Declare limitacoes honestamente

Ao finalizar: "Rode `audit-4-report` para gerar o relatorio final."
