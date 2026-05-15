---
name: release-setup
description: Configura versionamento automatico via release-please no projeto atual. Detecta se e single-package ou monorepo, adapta a estrategia (unificada vs per-package), cria workflow GitHub Actions, config files, manifest, e tags baseline. Use para projetos novos que ainda nao tem CHANGELOG/tags automatizados. Trigger quando o usuario mencionar "release setup", "setup de versao", "configurar versionamento", "release-please", "changelog automatico", "versionamento automatico", "skill de versao", "configurar release", ou qualquer variacao de iniciar versionamento.
---

# Release Setup — Versionamento automatico com release-please

Configura o sistema de releases automaticos num projeto. Detecta a estrutura (single vs monorepo, tipos de pacote) e adapta a config. Gera workflow GitHub Actions, config, manifest e tags baseline. Pergunta o minimo necessario.

---

## Fase 0 — Pre-requisitos

Rode em paralelo:

```bash
test -d .git && echo "GIT_OK" || echo "NO_GIT"
git remote -v 2>/dev/null
git log --oneline -5 2>/dev/null
test -f release-please-config.json && echo "ALREADY_SETUP"
```

**PARE** se:
- Nao for repo git → sugira `git-0-init`
- Nao tiver remote origin → avise e pergunte URL do GitHub
- Ja tiver `release-please-config.json` → pergunte se quer reconfigurar (e apague antes de continuar)

Se commits recentes nao seguem conventional commits (`feat:`, `fix:`, etc), avise que o sistema so funciona com esse padrao — usuario decide se continua.

---

## Fase 1 — Detectar estrutura do projeto

Varra o repo buscando arquivos de versao:

```bash
# Node
find . -maxdepth 3 -name "package.json" -not -path "*/node_modules/*" 2>/dev/null

# Java / Maven
find . -maxdepth 3 -name "pom.xml" 2>/dev/null

# Python
find . -maxdepth 3 \( -name "pyproject.toml" -o -name "setup.py" \) 2>/dev/null

# Rust
find . -maxdepth 3 -name "Cargo.toml" 2>/dev/null

# Go
find . -maxdepth 3 -name "go.mod" 2>/dev/null

# Scripts com APP_VERSION (desktop/custom)
grep -rlE '^(APP_VERSION|app\.versao|VERSION)=' --include="*.sh" --include="*.properties" 2>/dev/null | head -5
```

### Classificacao

| Detectou | Classificacao |
|----------|--------------|
| 1 `package.json` na raiz | **Single Node** |
| 1 `pom.xml` na raiz | **Single Maven** |
| 1 `pyproject.toml` na raiz | **Single Python** |
| 1 `Cargo.toml` na raiz | **Single Rust** |
| 2+ `package.json` em subpastas (raiz sem) | **Monorepo Node** |
| Mix de `pom.xml` + `package.json` em subpastas | **Monorepo Misto** |
| Arquivos de versao em subpastas diferentes | **Monorepo generico** |

### Mostre o mapa ao usuario

Apresente o que encontrou:

```
Detectei:
- apps/api (package.json v1.0.0)
- apps/web (package.json v0.1.0)
- Estrutura: Monorepo Node com 2 pacotes

Qual estrategia?
(A) Versao UNIFICADA — um numero pra tudo, 1 CHANGELOG, 1 tag por release
(B) Versao POR PACOTE — cada app tem seu ciclo, tags api-v1.0.0, web-v1.0.0, etc

Recomendo:
- A para apps que deployam juntos (ex: docker-compose unico)
- B para apps independentes com ciclos diferentes
```

**Single-package projects usam A automaticamente** (nao tem opcao).

---

## Fase 2 — Decisoes a perguntar

Independentemente da estrutura, pergunte:

1. **Baseline de versao**: use a versao mais alta detectada (ex: 1.0.0). Confirme ou pergunte.
2. **Incluir `perf:` como patch?** Recomende sim (padrao).
3. **Auto-merge do PR de release?** Pergunte:
    > "Quer que o PR de release seja mergeado automaticamente? (sim = projetos solo / prototipos, nao = projetos com code review obrigatorio)"
    - **Sim** → adiciona o step de auto-merge na Fase 3.1 (ver abaixo)
    - **Nao** (default seguro) → fluxo padrao: voce revisa e mergeia manualmente o PR que o bot abre
    - Se o repo tem branch protection exigindo review, auto-merge vai falhar — avise o usuario
4. **Pushar no final?** Pergunte no final do setup.

---

## Fase 3 — Gerar arquivos

### 3.1 — `.github/workflows/release.yml` (comum aos 2 modos)

**Versao padrao (merge manual):**

```yaml
name: release-please

on:
    push:
        branches: [main]

permissions:
    contents: write
    pull-requests: write

jobs:
    release-please:
        runs-on: ubuntu-latest
        steps:
            - uses: googleapis/release-please-action@v4
              with:
                  config-file: release-please-config.json
                  manifest-file: .release-please-manifest.json
```

**Versao com auto-merge** (se usuario escolheu auto-merge na Fase 2):

```yaml
name: release-please

on:
    push:
        branches: [main]

permissions:
    contents: write
    pull-requests: write

jobs:
    release-please:
        runs-on: ubuntu-latest
        steps:
            - uses: googleapis/release-please-action@v4
              id: release
              with:
                  config-file: release-please-config.json
                  manifest-file: .release-please-manifest.json

            - name: Auto-merge release PR
              if: ${{ steps.release.outputs.pr }}
              env:
                  GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
              run: |
                  PR_NUMBER=$(echo '${{ steps.release.outputs.pr }}' | jq -r '.number')
                  echo "Merging release PR #$PR_NUMBER"
                  gh pr merge "$PR_NUMBER" --squash --repo "${{ github.repository }}"
```

**Notas importantes sobre auto-merge:**
- `gh pr merge --squash` (sem `--auto`) mergeia imediatamente. Requer que o GITHUB_TOKEN tenha `contents: write` (ja setado) e que o repo **nao** tenha branch protection exigindo review.
- Para monorepo com `separate-pull-requests: true`, `steps.release.outputs.pr` ainda captura os PRs abertos nesse run. Se precisar mergear varios, use `steps.release.outputs.prs_created` + loop `jq -r '.[].number'`.
- Apos o merge, a workflow roda de novo (por causa do push de merge) e o release-please cria a tag + GitHub Release automaticamente.

Se a branch default nao for `main`, ajuste (ex: `master`).

### 3.2 — Modo UNIFIED (single-package ou monorepo + estrategia A)

**`release-please-config.json`:**

```json
{
    "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
    "include-v-in-tag": true,
    "packages": {
        ".": {
            "release-type": "simple",
            "package-name": "<nome-do-projeto>",
            "changelog-path": "CHANGELOG.md",
            "extra-files": [
                // Liste TODOS os arquivos de versao detectados como extra-files.
                // Use "json" com jsonpath "$.version" para package.json.
                // Use "pom" para pom.xml.
                // Use "generic" com marker "# x-release-please-version" para shell scripts.
            ]
        }
    },
    "changelog-sections": [ ... ] // padrao, ver Fase 4
}
```

**`.release-please-manifest.json`:**

```json
{
    ".": "<baseline-version>"
}
```

**Tag baseline unica:** `v<baseline>` (ex: `v1.0.0`) no HEAD atual.

### 3.3 — Modo PER-PACKAGE (monorepo + estrategia B)

**`release-please-config.json`:**

```json
{
    "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
    "include-component-in-tag": true,
    "separate-pull-requests": true,
    "packages": {
        "<path-do-pacote-1>": {
            "release-type": "<tipo>",
            "component": "<escopo-conventional-commit>",
            "package-name": "<nome>",
            "changelog-path": "CHANGELOG.md"
        },
        ...
    },
    "changelog-sections": [ ... ]
}
```

**Mapeamento tipico de `release-type`:**

| Linguagem | release-type |
|-----------|--------------|
| Node (package.json) | `node` |
| Maven (pom.xml) | `maven` |
| Python | `python` |
| Rust | `rust` |
| Go | `go` |
| Custom (shell/properties com marker) | `simple` + `extra-files` |

**Escolha do escopo (`component`)**: deve ser curto e match com o escopo do conventional commit. Ex: `naviera-api` → component `api` (usuario vai escrever `fix(api): ...`).

**`.release-please-manifest.json`:**

```json
{
    "<path-1>": "<baseline>",
    "<path-2>": "<baseline>",
    ...
}
```

**Tags baseline por componente:** `<component>-v<baseline>` (ex: `api-v1.0.0`, `web-v1.0.0`, ...).

### 3.4 — Arquivos custom com marker

Pra arquivos que nao sao package.json/pom.xml (ex: `build.sh` com `APP_VERSION="1.0.0"`), adicione o marker in-place:

```bash
# Linha unica
APP_VERSION="1.0.0" # x-release-please-version

# Bloco multi-linha (use quando comentario inline quebra a sintaxe, ex: .bat)
# x-release-please-start-version
APP_VERSION="1.0.0"
# x-release-please-end
```

Apenas em arquivos **tracked** pelo git. Se for gitignored, avise o usuario e nao inclua no config.

---

## Fase 4 — Changelog sections (padrao universal)

Use sempre esta secao em `release-please-config.json`:

```json
"changelog-sections": [
    { "type": "feat", "section": "Features" },
    { "type": "fix", "section": "Bug Fixes" },
    { "type": "perf", "section": "Performance" },
    { "type": "refactor", "section": "Refactoring" },
    { "type": "docs", "section": "Documentation" },
    { "type": "revert", "section": "Reverts" },
    { "type": "chore", "hidden": true },
    { "type": "test", "hidden": true },
    { "type": "style", "hidden": true },
    { "type": "build", "hidden": true },
    { "type": "ci", "hidden": true }
]
```

`feat` bumpa minor, `fix` e `perf` bumpam patch, `feat!` ou `BREAKING CHANGE` bumpam major.

---

## Fase 5 — Commit + tags baseline

1. Stage os arquivos criados + modificados (markers em build scripts, etc):
   ```bash
   git add .github/workflows/release.yml release-please-config.json .release-please-manifest.json
   # + arquivos com marker
   ```

2. Commit com mensagem:
   ```
   ci: add release-please for automated versioning
   ```

3. Criar tags baseline:
   - **Modo UNIFIED:** `git tag -a v<baseline> -m "v<baseline> - baseline for release-please"`
   - **Modo PER-PACKAGE:** loop por componente
     ```bash
     for c in <component1> <component2> ...; do
       git tag -a "$c-v<baseline>" -m "$c v<baseline> - baseline"
     done
     ```

4. **NAO push automatico** — pergunte no final.

---

## Fase 6 — Post-setup checklist (informe ao usuario)

Depois de commitar + taggar, mostre:

```
Setup concluido! Antes do primeiro release funcionar, voce precisa:

1. **Ativar permissao no GitHub** (1 vez so):
   https://github.com/<owner>/<repo>/settings/actions
   
   Workflow permissions:
   - [x] Read and write permissions
   - [x] Allow GitHub Actions to create and approve pull requests
   - Clique Save

2. **Pushar** (quando quiser):
   git push origin <branch>
   git push origin --tags

3. **Primeiro commit de verdade** com conventional commits:
   fix(<escopo>): ...  → bumpa patch
   feat(<escopo>): ... → bumpa minor
   feat!(<escopo>): ... ou BREAKING CHANGE: → major

4. O bot vai abrir PR "chore(<escopo>): release <nome> <versao>" automatico.
   - **Merge manual** (padrao): voce revisa o CHANGELOG, mergeia, tag e criada.
   - **Auto-merge** (se habilitado na Fase 2): PR e mergeado imediatamente; workflow roda de novo e cria a tag + GitHub Release sozinho.
```

Inclua os componentes/tags criados no resumo. Se auto-merge esta ligado, deixe claro que o fluxo e **push → tag + release, sem intervencao manual**.

---

## Casos especiais

### Repo sem remote

Se `git remote -v` retornar vazio, pergunte o URL e configure:
```bash
git remote add origin <url>
```
Mas avise que o workflow so dispara apos o primeiro push.

### Branch default diferente de main

Detecte:
```bash
git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@'
# OU fallback:
git branch --show-current
```

Ajuste o `branches:` no workflow YAML.

### Projeto ja commitou usando `feat!:` ou `BREAKING CHANGE` antes do setup

Primeiro run do release-please vai propor major bump ao escanear historia. Opcoes:

1. **Aceitar** o major na primeira release (semver honesto)
2. **Bootstrap as tags no HEAD** (como fizemos aqui) pra zerar o contador — futuras releases partem do baseline sem historia antiga

Padrao: **bootstrap tags no HEAD**. So ofereca opcao 1 se o usuario perguntar.

### Gitignore bloqueia arquivo de versao

Se um arquivo que voce quer bumpar ta no gitignore:
- Avise o usuario
- Pergunte se quer remover do gitignore ou se prefere deixar de fora do versionamento automatico
- Nao tente force-add sem autorizacao
