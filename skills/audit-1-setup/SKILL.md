---
name: audit-1-setup
description: Prepara o ambiente para uma auditoria de codigo. Cria a estrutura de pastas docs/, determina a versao do proximo relatorio, e faz o mapeamento estrutural completo do projeto. Use esta skill ANTES de qualquer auditoria. Trigger quando o usuario mencionar "audit setup", "preparar auditoria", "iniciar auditoria", "audit-setup", "mapear projeto para auditoria", ou qualquer variacao de iniciar/preparar uma auditoria de codigo.
---

# Audit Setup — Preparacao e Mapeamento Estrutural

Prepara o terreno para uma auditoria. Define versionamento e mapeia estrutura.

## Etapa 1 — Estrutura de pastas

Crie se nao existir (nao sobrescreva arquivos existentes):

```
docs/
├── STATUS.md
├── audits/
│   ├── current/
│   ├── archive/
│   └── temp/
├── mvp/
│   ├── current/
│   └── archive/
├── decisions/
├── runbooks/
└── specs/
```

Se STATUS.md nao existir, crie com header basico:
```markdown
# STATUS DO PROJETO — [NOME]
> Ultima atualizacao: [data]
## Estado Geral: EM CONFIGURACAO
Estrutura de documentacao criada. Primeiro audit pendente.
```

## Etapa 2 — Versionamento

1. Busque `AUDIT_V*.md` em `docs/audits/current/` e `docs/audits/archive/`.
2. Se houver audits legados em outros locais (`docs/`, `/audits/`), considere no historico.
3. Determine proxima versao:
   - Nenhum existe: V1.0
   - Ultimo minor < 10: incrementa minor (V1.2 -> V1.3)
   - Ultimo minor = 10: incrementa major (V1.10 -> V2.0)
4. Salve em `docs/audits/temp/version.txt`.

## Etapa 3 — Mapeamento Estrutural

Mapeie o projeto:

1. **Arvore de diretorios** — pastas e responsabilidades (ignore node_modules, .git, dist, build)
2. **Entrypoints** — onde a aplicacao inicia
3. **Dependencias** — principais com versoes
4. **Fluxo de dados** — caminho de uma request tipica
5. **Variaveis de ambiente** — leia .env.example ou refs a process.env

Salve em `docs/audits/temp/00_setup.md`:

```markdown
# Audit Setup — [NOME_DO_PROJETO]
> Versao planejada: V[X.Y]
> Data: [YYYY-MM-DD]
> Stack: [stack]

## Arvore do Projeto
[arvore]

## Pontos de Entrada
[entrypoints]

## Dependencias Principais
| Dependencia | Versao | Proposito |
|------------|--------|-----------|

## Fluxo de Dados Principal
[descricao]

## Variaveis de Ambiente
| Variavel | Obrigatoria | Default | Descricao |
|----------|-------------|---------|-----------|
```

## Regras

- NAO corrija codigo, NAO de sugestoes, NAO audite
- Apenas mapeie e documente

Ao finalizar: "Setup concluido. Rode `audit-2-scan` para iniciar a auditoria."
