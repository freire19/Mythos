#!/usr/bin/env bash
# sync-upstream.sh — puxa atualizacoes do Alpha_Code mantendo a identidade do Mythos.
#
# Versao mecanica/standalone. Para o fluxo interativo conduzido pelo Claude, use a
# skill /git-sync-mythos. Os dois sao equivalentes — esta versao serve pra rodar
# sem o Claude (CI, automacao, ou pra ter rapidez sem perguntas).
#
# Pre-requisitos:
#   - Working tree limpa
#   - Branch master atual
#   - Remote alpha-upstream apontando pra Alpha_Code.git
#   - git config merge.ours.driver true  (rodar uma vez por clone)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
blue()   { printf '\033[34m%s\033[0m\n' "$*"; }

# --- 1. Pre-flight ---------------------------------------------------------

[ -n "$(git status --porcelain)" ] && { red "Working tree nao esta limpa. Commit/stash antes."; exit 1; }

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT_BRANCH" = "master" ] || { red "Voce esta na branch '$CURRENT_BRANCH'. Mude pra master."; exit 1; }

git remote get-url alpha-upstream >/dev/null 2>&1 \
  || { red "Remote 'alpha-upstream' nao existe. Rode: git remote add alpha-upstream https://github.com/freire19/Alpha_Code.git"; exit 1; }

# Avisa se o merge driver 'ours' nao esta configurado (silenciosamente o .gitattributes nao funciona).
git config --get merge.ours.driver >/dev/null \
  || yellow "AVISO: merge.ours.driver nao configurado. Rode: git config merge.ours.driver true"

# --- 2. Fetch --------------------------------------------------------------

blue "Buscando alpha-upstream/master..."
git fetch alpha-upstream master --tags

NEW_COMMITS="$(git log --oneline master..alpha-upstream/master | wc -l)"
[ "$NEW_COMMITS" -eq 0 ] && { green "Ja esta atualizado. Nada a fazer."; exit 0; }

blue "$NEW_COMMITS commit(s) novo(s) do Alpha_Code:"
git log --oneline master..alpha-upstream/master

# --- 3. Confirmacao --------------------------------------------------------

read -r -p "Continuar com o merge? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || { yellow "Abortado."; exit 0; }

# --- 4. Branch de sync -----------------------------------------------------

UPSTREAM_SHA="$(git rev-parse --short alpha-upstream/master)"
UPSTREAM_TAG="$(git describe --tags alpha-upstream/master 2>/dev/null || echo "$UPSTREAM_SHA")"
SYNC_BRANCH="sync/alpha-code-${UPSTREAM_TAG}"

git checkout -b "$SYNC_BRANCH" master
blue "Branch criada: $SYNC_BRANCH"

# --- 5. Merge --------------------------------------------------------------

blue "Fazendo merge..."
if ! git merge --no-edit --no-ff alpha-upstream/master -m "merge: sync Alpha_Code ${UPSTREAM_TAG} into Mythos"; then
  red "Conflitos detectados. Resolva manualmente e rode 'git merge --continue'."
  red "Arquivos em conflito:"
  git diff --name-only --diff-filter=U
  exit 1
fi

# --- 6. Sanidade da identidade --------------------------------------------

blue "Verificando integridade da identidade Mythos..."
FAILED=0
check() {
  local desc="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    green "  OK: $desc"
  else
    red "  FALHOU: $desc"
    FAILED=1
  fi
}

check "pyproject.toml tem name=\"mythos\""     "grep -q 'name = \"mythos\"' pyproject.toml"
check "pyproject.toml tem entry mythos=main"   "grep -q 'mythos = \"main:main\"' pyproject.toml"
check "prompts/system.md menciona MYTHOS"      "grep -q 'MYTHOS' prompts/system.md"
check "README.md comeca com '# Mythos'"        "head -1 README.md | grep -q '^# Mythos'"
check "ALPHA.md referencia 'Mythos repo'"      "grep -q 'Mythos repo' ALPHA.md"
check "skills/security-audit existe"           "test -d skills/security-audit"
check "skills/exploit-development existe"      "test -d skills/exploit-development"
check "alpha/tools/red_tools.py existe"        "test -f alpha/tools/red_tools.py"
check "alpha/security.py existe"               "test -f alpha/security.py"

if [ "$FAILED" -ne 0 ]; then
  red "Identidade comprometida apos merge. Inspecione o diff antes de prosseguir."
  red "Branch '$SYNC_BRANCH' mantida pra investigacao."
  exit 1
fi

# --- 7. Testes -------------------------------------------------------------

if command -v pytest >/dev/null 2>&1; then
  blue "Rodando pytest..."
  if ! pytest -x --tb=short; then
    red "Testes falharam. Branch '$SYNC_BRANCH' mantida pra investigacao."
    exit 1
  fi
else
  yellow "pytest nao encontrado, pulando testes."
fi

# --- 8. Suspeitos pra revisao manual --------------------------------------

blue "Arquivos novos vindos do upstream que mencionam 'Alpha Code' / 'alpha-code' (revisao opcional de branding):"
git diff --name-only --diff-filter=A master..HEAD | while read -r f; do
  if [ -f "$f" ] && grep -l -i "alpha[_ -]code" "$f" >/dev/null 2>&1; then
    echo "  - $f"
  fi
done

# --- 9. Finalizacao --------------------------------------------------------

read -r -p "Tudo verificado. Fazer fast-forward do master pra '$SYNC_BRANCH' e push? [y/N] " FINAL
if [[ "$FINAL" =~ ^[Yy]$ ]]; then
  git checkout master
  git merge --ff-only "$SYNC_BRANCH"
  git push origin master
  git branch -d "$SYNC_BRANCH"
  green "Sync concluido. Master atualizado com Alpha_Code ${UPSTREAM_TAG}."
else
  yellow "Branch '$SYNC_BRANCH' mantida. Quando quiser:"
  yellow "  git checkout master && git merge --ff-only $SYNC_BRANCH && git push origin master"
fi
