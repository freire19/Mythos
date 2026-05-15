---
name: mvp-1-check
description: 'Analisa um projeto existente e identifica tudo que falta para ele rodar como MVP funcional. Verifica funcionalidades core, fluxos criticos, infra, seguranca minima, estabilidade, UX e dependencias. Suporta escopo global (projeto todo) ou focado em uma camada/subprojeto especifico (ex: api, web, app, desktop, ocr) em monorepos. Use quando o usuario mencionar "mvp check", "mvp-check", "verificar mvp", "o que falta pro mvp", "mvp readiness", "checar se ta pronto", "o que falta pra rodar", "validar mvp", "mvp do <camada>", ou qualquer variacao.'
---

# MVP Check — Verificacao de Prontidao

Analisa projeto existente e identifica gaps entre estado atual e MVP funcional. Funciona em projetos single-package OU em monorepos com varias camadas/subprojetos, com escopo configuravel.

## Conceito

MVP nao e meia boca. E a menor versao que entrega valor real e funciona de forma confiavel. Pode faltar: features secundarias, UI polida, analytics. NAO pode faltar: fluxos core, error handling basico, seguranca minima, deploy funcional.

## Etapa 0 — Identificar projeto e escopo

### 0.1 Identificar o projeto
- Working directory atual = projeto. Le `CLAUDE.md` (se existir) e `README.md` raiz para extrair nome do projeto e arquitetura.
- Confirme com o usuario se o nome detectado faz sentido (ex: "Projeto detectado: Naviera Eco. Confirma?"). Se ja estiver claro pelo `CLAUDE.md`, pule a confirmacao e prossiga.

### 0.2 Detectar layout (single vs multi)

Heuristicas (rode em paralelo via Bash):

```bash
# Submodulos npm (excluindo node_modules)
find . -maxdepth 3 -name "package.json" -not -path "*/node_modules/*" -not -path "*/dist/*"

# Submodulos Java/Maven
find . -maxdepth 3 -name "pom.xml"

# Submodulos Gradle
find . -maxdepth 3 -name "build.gradle*"

# Submodulos Python
find . -maxdepth 3 -name "pyproject.toml" -o -name "setup.py" -o -name "requirements.txt"

# Pastas comuns de monorepo
ls -d */ 2>/dev/null | grep -iE "^(apps?|services?|packages|modules|backend|frontend|server|client|web|api|app|mobile|desktop|admin|ocr)/?$"

# docker-compose com varios services
grep -E "^  [a-z][a-z0-9_-]+:$" docker-compose*.yml 2>/dev/null | head -20
```

Se houver **2+ unidades** (excluindo a raiz e node_modules), e **multi-package**.

### 0.3 Definir escopo

**Se single-package:** escopo = `ALL` (analise o projeto inteiro). Pule para Etapa 1.

**Se multi-package:**

1. Se o usuario passou argumento (ex: `/mvp-1-check naviera-app` ou `/mvp-1-check api`):
   - Tente fazer match com as unidades detectadas (case-insensitive, prefixo ou sufixo).
   - Se match unico: use como escopo. Avise: "Escopo detectado: <nome>. Iniciando analise."
   - Se match ambiguo: liste as opcoes e peca confirmacao.

2. Se sem argumento, **pergunte ao usuario** com as opcoes detectadas:

   ```
   Detectei multiplas camadas neste projeto:

   1) ALL — projeto inteiro (todas as camadas)
   <para cada unidade detectada>
   N) <nome-da-pasta> — <tecnologia detectada, ex: Spring Boot API / React + Vite / JavaFX desktop>

   Qual escopo da analise? (digite o numero ou o nome)
   ```

   Aguarde resposta antes de prosseguir.

3. Salve o escopo escolhido em `docs/mvp/temp/mvp_scope.txt`:
   - `ALL` para projeto inteiro
   - Nome da pasta para escopo focado (ex: `naviera-app`, `naviera-api`)

### 0.4 Ajustar versao para o escopo

Versionamento e **por escopo** — analises focadas nao sobrescrevem a versao da analise geral.

- **Escopo ALL:** versao convencional (V1.0, V2.0, ...). Verifique `docs/mvp/current/MVP_PLAN.md` e `docs/mvp/archive/MVP_PLAN_V*.md` (sem sufixo).
- **Escopo focado (ex: naviera-app):** versao com sufixo `-<SCOPE>`. Verifique `docs/mvp/current/MVP_PLAN_<SCOPE_UPPER>.md` e `docs/mvp/archive/MVP_PLAN_<SCOPE_UPPER>_V*.md`.

Salve em `docs/mvp/temp/mvp_version.txt`:
```
<versao>
Escopo: <ALL ou nome-do-subprojeto>
```

## Etapa 1 — Preparacao

1. Crie `docs/mvp/temp/` se nao existir.
2. Confirme `mvp_scope.txt` e `mvp_version.txt` salvos.
3. Identifique os arquivos relevantes para o escopo:
   - **ALL:** projeto inteiro
   - **Focado:** somente arquivos sob a pasta do escopo (ex: `naviera-app/**`). Arquivos externos so entram quando sao dependencia critica e claramente identificada (ex: `docker-compose.yml` referenciando o subprojeto).

## Etapa 2 — Analise (7 dimensoes)

Classifique cada item: **PRONTO | INCOMPLETO | FALTANDO | POS-MVP**

Quando o escopo for focado, **analise APENAS** o subprojeto. Mencione interfaces externas (APIs consumidas, etc) como dependencias, nao como itens do escopo.

### Dim 1 — Funcionalidades Core → `docs/mvp/temp/01_features.md`
Identifique features que sao a razao de existir do projeto (ou da camada). Para cada: happy path funciona? Edge cases criticos tratados? Usuario completa sem ajuda?

### Dim 2 — Fluxos Criticos → `docs/mvp/temp/02_flows.md`
Onboarding/primeiro uso, fluxo principal de ponta a ponta, tratamento de erros do usuario, fluxo de saida/encerramento.

### Dim 3 — Infraestrutura → `docs/mvp/temp/03_infra.md`
Dockerfile/docker-compose, script de deploy/CI/CD, .env.example, migrations, setup com comando unico, README com instrucoes, logs de producao, healthcheck.

### Dim 4 — Seguranca Minima → `docs/mvp/temp/04_security.md`
Autenticacao, secrets fora do codigo, inputs validados, HTTPS, rate limiting basico, dados sensiveis nao vazam.

### Dim 5 — Estabilidade → `docs/mvp/temp/05_stability.md`
Error handling nos fluxos core, reconexao com banco/servicos, graceful shutdown, timeouts, recuperacao automatica de erros transientes.

### Dim 6 — UX Minima → `docs/mvp/temp/06_ux.md`
Feedback de acoes (loading/sucesso/erro), mensagens de erro uteis, fluxo intuitivo, responsividade basica (se web).

### Dim 7 — Dependencias → `docs/mvp/temp/07_dependencies.md`
Dependencias pinadas, APIs externas funcionando, servicos de terceiros acessiveis, fallback se externo cair.

> **Dim irrelevante para o escopo:** se uma dimensao nao se aplica (ex: UX em uma API headless), gere o arquivo com nota explicativa "N/A para esta camada — UX e responsabilidade do consumidor". Nao apague o arquivo.

## Etapa 3 — Resumo

Salve em `docs/mvp/temp/08_summary.md`:

```markdown
# Resumo de Gaps — <NOME_DO_PROJETO> (<ESCOPO>)

| Status | Quantidade |
|--------|-----------|
| PRONTO | X |
| INCOMPLETO | X |
| FALTANDO | X |
| POS-MVP | X |

## Bloqueadores de lancamento
[FALTANDO + INCOMPLETO criticos]

## Estimativa de esforco
[horas/dias para resolver bloqueadores]
```

Ao finalizar:
- Reporte: contagem de itens por status, top 3 bloqueadores, escopo da analise.
- Sugira: "Rode `mvp-2-report` para gerar o plano de acao." (Mencione que o report respeitara o escopo desta analise.)

## Principios

- Seja pragmatico: happy path funciona e nao crasha pode ser suficiente
- Seja honesto: se algo critico falta, diga
- Nao sugira features novas — foco no que existe
- Em escopo focado, NAO comente sobre outras camadas que nao foram pedidas. Mantenha-se dentro do escopo.
