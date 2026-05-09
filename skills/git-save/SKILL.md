---
name: git-save
description: Commit + Push em um unico comando. Combina git-1-commit e git-2-push automaticamente. Em monorepos com release-please per-package (separate-pull-requests=true), divide as mudancas em commits separados por componente (um commit por pacote com escopo). Trigger quando o usuario mencionar "save", "git save", "salvar e enviar", "commit e push", "git-save", ou qualquer variacao de commitar e pushar junto.
---

# Git Save — Commit + Push em um comando

Combina o fluxo completo de commit seguro + push para o GitHub.

**Suporta 2 modos:**
- **Unificado** — um commit so para todas as mudancas (repo simples ou release-please de versao unica)
- **Per-package** — um commit por pacote com escopo `(component)`, detectado quando `release-please-config.json` tem `"separate-pull-requests": true`

---

## Fase 0 — Sincronizar com remote (git pull)

**ANTES de qualquer commit, garanta que o local esta atualizado.**

```bash
git fetch origin $(git branch --show-current) 2>/dev/null
git log HEAD..origin/$(git branch --show-current) --oneline 2>/dev/null
```

Se houver commits novos no remote:
```bash
git pull origin $(git branch --show-current)
```

Se houver conflito: **PARE** e avise o usuario. Nunca force.

---

## Fase 1 — Detectar estrategia de commit

Verifique se o repo usa release-please per-package:

```bash
test -f release-please-config.json && \
  grep -q '"separate-pull-requests"[[:space:]]*:[[:space:]]*true' release-please-config.json && \
  echo "MULTI-PACKAGE" || echo "UNIFIED"
```

- **UNIFIED** → siga a Fase 4A (commit unico)
- **MULTI-PACKAGE** → siga a Fase 4B (N commits por pacote)

Se for MULTI-PACKAGE, leia a config para mapear `path → component`:

```bash
cat release-please-config.json
```

Extraia os pacotes do campo `packages`. A chave do pacote e o **path** no repo. O campo `component` e o **escopo** do conventional commit.

Exemplo Naviera_Eco:
```
"naviera-api"  → api     (fix(api): ...)
"naviera-web"  → web
"naviera-app"  → app
"build"        → desktop
```

---

## Fase 2 — Diagnostico

Rode em paralelo:

1. `git status` (nunca use -uall)
2. `git diff --stat` (resumo das mudancas staged + unstaged)
3. `git log --oneline -5` (ultimos commits para manter estilo)

Se nao houver mudancas de codigo fonte (ignorar bin/): diga "Nada para salvar." e pare.

---

## Fase 3 — Verificacao de Seguranca

**CRITICO: Faca ANTES de qualquer `git add`.**

1. Verifique se `.gitignore` protege arquivos sensiveis:
   ```
   git check-ignore .env backend/.env *.key *.pem credentials*.json
   ```

2. Procure arquivos sensiveis nos untracked/modified:
   - `.env` (qualquer variacao)
   - `*credentials*`, `*secret*`, `*.key`, `*.pem`
   - Arquivos com tokens/API keys no conteudo
   - Service account JSONs (`gen-lang-client-*.json`, etc.)
   - Databases (`.db`, `.sqlite`)

3. Se encontrar arquivo sensivel NAO protegido pelo .gitignore:
   - **PARE** e avise o usuario
   - Sugira adicionar ao .gitignore
   - So prossiga depois de confirmar protecao

---

## Fase 4A — Staging e Commit (modo UNIFIED)

### Etapa 1 — Staging

1. Analise todas as mudancas e agrupe logicamente
2. Use `git add` com arquivos especificos (evite `git add -A` ou `git add .`)
3. Nunca stage arquivos que contem secrets/tokens, sao databases/caches, ou binarios grandes

### Etapa 2 — Commit

Siga o padrao **Conventional Commits**:

```
<tipo>: <descricao curta e objetiva>

<corpo opcional — o que mudou e por que>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

**Tipos:** feat, fix, refactor, docs, test, chore, perf, style

**Regras:**
- Titulo em ingles, max 72 caracteres
- Foco no "por que", nao no "o que"
- SEMPRE inclua o `Co-Authored-By` no final

**Use HEREDOC:**
```bash
git commit -m "$(cat <<'EOF'
feat: descricao aqui

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Se pre-commit hook falhar: corrija, re-stage, crie **NOVO** commit (nunca --amend).

---

## Fase 4B — Staging e Commit (modo MULTI-PACKAGE)

### Etapa 1 — Agrupar arquivos por pacote

Para cada arquivo modificado (`git status --short`), determine a qual pacote pertence:

- Se o caminho comeca com `<package-path>/` → pertence ao componente `<component>`
- Caso contrario (docs raiz, `.github/`, `CLAUDE.md`, `README.md`, etc.) → cross-cutting

**Regra de desempate:** para codigo que nao esta fisicamente em `<package-path>/` mas pertence logicamente a um componente (ex: `src/` no Naviera_Eco pertence ao **desktop** mesmo estando fora de `build/`), **PERGUNTE ao usuario** qual escopo usar antes de commitar. Nao adivinhe.

### Etapa 2 — Mostrar o plano ao usuario

Apresente um breakdown dos commits propostos ANTES de executar:

```
Vou criar N commits:

1. fix(api): <descricao> — 3 arquivos em naviera-api/
2. feat(web): <descricao> — 5 arquivos em naviera-web/
3. chore: <descricao> — 1 arquivo em .github/, 1 em docs/ (cross-cutting)

Confirma?
```

Espere confirmacao antes de prosseguir.

### Etapa 3 — Criar commits em sequencia

Para cada grupo:

1. Stage so os arquivos daquele pacote: `git add <file1> <file2> ...`
2. Determine o tipo (feat/fix/refactor/etc) baseado nas mudancas **daquele pacote**
3. Commit com escopo:
   ```
   <tipo>(<component>): <descricao>
   
   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   ```
4. Repita ate todos os grupos commitados

**Cross-cutting** (arquivos fora de qualquer pacote): commit sem escopo, normalmente `chore:`, `docs:` ou `ci:`.

### Etapa 4 — Validacao

Apos todos os commits:
```bash
git log --oneline -N  # N = numero de commits criados
```

Confirme que cada commit tem escopo correto e esta na ordem esperada.

---

## Fase 5 — Push

1. Verifique remote: `git remote -v`
2. Verifique branch: `git branch --show-current`
3. Se branch remota divergiu: `git stash && git pull --rebase origin <branch> && git stash pop`
4. Execute: `git push -u origin <branch>`

**NUNCA faca force push sem autorizacao explicita.**
**NUNCA faca force push para main/master.**

---

## Fase 6 — Confirmacao

### Modo UNIFIED
> Salvo! `<hash>` — <mensagem> | <N> arquivo(s) | <branch> → origin

### Modo MULTI-PACKAGE
> Salvo! `<N>` commits criados e pushados:
> - `<hash1>` fix(api): ...
> - `<hash2>` feat(web): ...
> - `<hash3>` chore: ...
>
> Branch: <branch> → origin
>
> **Release-please deve abrir <M> PRs de release em ~30s** (um por componente com mudanca que bumpa).

Inclua a URL do repositorio.
