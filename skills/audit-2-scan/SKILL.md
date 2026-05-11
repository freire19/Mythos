---
name: audit-2-scan
description: 'Executa a auditoria destrutiva completa do codigo como um dev senior. Varre o projeto em 7 categorias — bugs criticos, seguranca (com npm audit), logica, resiliencia, performance, manutenibilidade, testes. Salva resultados parciais em docs/audits/temp/. Use DEPOIS do audit-1-setup. Trigger quando o usuario mencionar "audit scan", "rodar auditoria", "escanear codigo", "audit-scan", "auditar projeto", "buscar bugs", "code review completo", ou qualquer variacao. Tambem trigger para categoria especifica: "auditar apenas seguranca".'
---

# Audit Scan — Auditoria Destrutiva de Codigo

Postura de dev senior com 15+ anos fazendo code review destrutivo antes de deploy em producao. Objetivo: encontrar problemas, nao elogiar codigo.

## Pre-requisito

Verifique se `docs/audits/temp/00_setup.md` existe. Se nao: "Rode `audit-1-setup` primeiro." e pare.
Leia `docs/audits/temp/00_setup.md` para entender a estrutura.

## Postura

- Seja destrutivo: seu trabalho e achar defeitos
- Nao invente problemas — categoria limpa = diga explicitamente
- Nao ignore configs (.env.example, Dockerfile, CI/CD, docker-compose)
- Se nao cobrir tudo, declare o que ficou de fora

## Formato de cada issue

```
#### Issue #[NNN] — [Titulo descritivo]
- **Severidade:** [CRITICO | ALTO | MEDIO | BAIXO]
- **Arquivo:** `caminho/do/arquivo`
- **Linha(s):** XX-YY
- **Problema:** [descricao objetiva]
- **Impacto:** [o que acontece se nao corrigir]
- **Codigo problematico:**
\`\`\`[linguagem]
// trecho real
\`\`\`
- **Fix sugerido:**
\`\`\`[linguagem]
// codigo corrigido
\`\`\`
```

Numere issues sequencialmente (#001, #002...) sem reiniciar entre categorias.

## Categorias (executar em sequencia, salvar cada uma)

### Cat 1 — Bugs Criticos e Runtime → `docs/audits/temp/01_bugs.md`
Race conditions, deadlocks, memory leaks, null/undefined nao tratados, loops infinitos, promises sem .catch, async sem try/catch, tipos incorretos, imports quebrados/circulares, index out of bounds, division by zero.

### Cat 2 — Seguranca → `docs/audits/temp/02_security.md`
Injecao (SQL, NoSQL, XSS, CSRF, command), secrets hardcoded, .env commitado, inputs sem sanitizacao, endpoints sem auth, dependencias com CVEs, headers ausentes (CORS, CSP, HSTS), rate limiting ausente, dados sensiveis em logs/responses, tokens sem expiracao, uploads sem validacao.

**Obrigatorio**: rodar `npm audit` (ou `yarn audit` / `pnpm audit` / `pip audit` / equivalente ao package manager do projeto) e reportar vulnerabilidades CRITICAS e ALTAS como issues individuais — incluir CVE, pacote, versao afetada e versao fixada.

### Cat 3 — Logica de Negocio → `docs/audits/temp/03_logic.md`
Condicionais sem edge cases, regras inconsistentes entre modulos, estados impossiveis, validacoes ausentes, transacoes sem rollback, operacoes nao-atomicas, ordem de execucao nao garantida, valores monetarios em float.

### Cat 4 — Resiliencia e Error Handling → `docs/audits/temp/04_resilience.md`
Try/catch que engolem erros, APIs externas sem retry/fallback, requests sem timeout, erros sem contexto, falta de circuit breaker, falta de graceful shutdown, processos que morrem silenciosamente, webhooks sem idempotencia.

### Cat 5 — Performance → `docs/audits/temp/05_performance.md`
Queries N+1, operacoes O(n2)+, falta de cache, payloads sem paginacao, conexoes sem pool, arquivos grandes sem stream, regex catastrofico, logs sincronos em hot paths, falta de indices.

### Cat 6 — Manutenibilidade → `docs/audits/temp/06_maintainability.md`
Codigo duplicado, funcoes >50 linhas, acoplamento forte, nomes enganosos, dead code, TODO/FIXME/HACK abandonados, magic numbers, configs inconsistentes, arquivos >500 linhas.

### Cat 7 — Testes → `docs/audits/temp/07_tests.md`
Rode a suite (`npm test` / `vitest run` / `jest` / `pytest` / equivalente) — reporte falhas.
Procure: `.skip()` / `.only()` / `xdescribe` / `xit` commitados, assercoes fracas (`expect(x).toBeTruthy()` onde deveria ser comparacao exata), testes sem `await` em operacao async, mocks de implementacao interna em vez de boundary, fixtures duplicadas que deveriam vir de factory, tempo de teste > 100ms em unit, flaky tests (dependem de ordem, timers reais, data), cobertura < threshold declarado, paths criticos sem teste (ex: error handling, edge cases, fluxos de pagamento), snapshots obsoletos ou gigantes.

Se nao encontrar nada em uma categoria: escreva "Nenhum problema encontrado." no arquivo.

## Ao finalizar

Informe:
1. Quantas issues por categoria
2. Quantas CRITICAS
3. "Rode `audit-3-review` para contra-verificar."

## Projeto grande

Cubra com profundidade o que conseguir. Adicione "Arquivos nao cobertos" ao final de cada arquivo. Sugira rodar novamente nos diretorios restantes.
