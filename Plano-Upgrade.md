# Plano de Upgrade — Alpha Code

> Documento de direção estratégica. Não é checklist de bugs (isso vive em `docs/STATUS.md`); é o **caminho de "indie tool madura" para "produto único e distribuído"**.

---

## Diagnóstico em uma linha

Alpha Code já é um Claude Code self-hosted bem-feito (17k LOC, 7k em testes, 6 audits zerados de CRÍTICO/ALTO, 5 providers, MCP, multi-agent). Subir de nível **não é consertar** — é escolher direção. Este plano cobre três frentes: **arquitetura sustentável**, **diferenciação real**, **distribuição**.

---

## Decisão estratégica que precede tudo

**Alpha Code é ferramenta pessoal polida ou produto para outras pessoas?**

- Os audits dizem "produto" (esse rigor não se justifica para uso solo).
- Mas a falta de PyPI, README magro de features avançadas e invisibilidade de multi-agent dizem "pessoal".

Resolver isso **antes** de executar o plano. Se for pessoal, foco em §3 (diferenciação que melhora *seu* fluxo). Se for produto, §4 e §5 antes de §3.

---

## §1. Arquitetura — três pontos que vão doer em 6 meses

### 1.1 `display/core.py` (1162 linhas, issue #009)
**Problema:** god-object de UI. Cada feature visual passa por aqui — bloqueia paralelizar trabalho e tornar testável.
**Solução:** quebrar em `alpha/display/renderers/{plan_card,todo_list,tool_call,approval,streaming}.py` + um `Theme` central.
**Custo real:** 6–8h (não 4h como STATUS sugere).
**Ganho:** desbloqueia evolução visual sem medo de regressão.

### 1.2 Loop do agente acoplado a OpenAI tool-calling
**Problema:** `alpha/agent/__init__.py:run_agent` chama `stream_chat_with_tools` que assume formato OpenAI. Adapter Anthropic vive em `llm_anthropic.py` selecionado por `api_format` na config — qualquer provider novo com formato diferente (Gemini nativo, Bedrock Converse) é if/elif no caminho quente.
**Solução:** extrair `ProviderProtocol` com (`stream`, `format_tools`, `parse_tool_calls`, `build_assistant_message`) e mover `llm.py`/`llm_anthropic.py` para `alpha/providers/{openai,anthropic,gemini,…}/`.
**Custo:** 1 dia.
**Ganho:** adicionar Gemini/Bedrock vira escrever 1 arquivo, não tocar o loop.

### 1.3 Tools sem isolamento de processo
**Problema:** executor roda tools no mesmo event loop. Allowlist de 75 comandos para `execute_shell` é boa, mas tool malicioso compartilha memória/FDs/env com o agente. `write_file` em `.git/hooks/post-commit` é execução latente.
**Solução:** modo `--sandbox` opcional. Tools `DESTRUCTIVE` rodam em subprocess com seccomp/landlock (Linux) ou ao menos com env limpo + `chdir(workspace)`. Não precisa ser default — precisa **existir** para CI/agentes longos.
**Custo:** 2 dias.
**Ganho:** habilita uso em ambientes não-confiados.

---

## §2. Dívidas pendentes com priorização revista

### 2.1 #002 (write_file + execute_shell plant+execute) — subir prioridade
Está MÉDIO há tempo. É o vetor de prompt injection mais óbvio hoje: modelo escreve script com `write_file` (auto-aprovado) em `~/.bashrc` ou `.git/hooks/post-commit`, depois espera execução latente.

**Fix mínimo (não 2h, 30 min):** gating por path em `write_file` — caminhos sensíveis (`~/.*rc`, `.git/hooks/*`, `.alpha/settings.json`, qualquer coisa fora de `workspace`) pedem aprovação. Reaproveita a mesma máquina de `needs_approval`.

### 2.2 #007 (SequenceMatcher cache) — perfilar antes
Loop detection roda toda iteração; `SequenceMatcher` é O(n·m). Antes de cachear cego, rode `python -X importtime + cProfile` em sessão real e veja se aparece. Se aparecer, `@lru_cache` nos pares ordenados é trivial (5 min).

### 2.3 #011 (`safe_json_loads` duplicado em 8 módulos) — fechar
É baixo, mas o tipo de débito que silenciosamente cria 8 comportamentos sutilmente diferentes. **30 min** para centralizar em `alpha/_json_utils.py`.

### 2.4 Testes — o que falta medir
- **Coverage real.** Não vi no CI. Adicionar `pytest-cov` + threshold mínimo (60% inicial, subir progressivamente).
- **Snapshot do system prompt.** `prompts/system.md` é o ativo mais crítico e o menos testado. Snapshot test com `syrupy` ou similar.
- **Canário multi-provider.** Mesma sequência mockada nos 5 adapters → histórico final equivalente. Pega bug de adapter cedo.

---

## §3. Diferenciação — onde Alpha vira único

### 3.1 Memory persistente cross-session (lock-in feature)
Skills hoje são estáticas em `skills/` + `~/.alpha/skills/`. Adicionar camada `~/.alpha/memory/` onde o agente registra: padrões do projeto, comandos que falharam, preferências do user, feedbacks. Expor em `/memory list|forget|edit`.

Esse é o feature que cria *retenção*: usuário não migra porque o assistente "conhece" ele.

**Custo:** 1 dia. **Ganho:** diferenciação clara vs Aider/Codex.

### 3.2 `delegate_consensus` (aproveita multi-agent enterrado)
`delegate_parallel` com 3 sub-agentes em workspaces isolados é poderoso e quase ninguém oferece. Mas: (1) está enterrado no README, (2) falta *coordinator pattern* — hoje o pai decide fan-out, sem reduce automático.

Adicionar `delegate_consensus(question, agents=N)` → pede a N agents e retorna majority/disagreement. Único na categoria. Funciona para audits, code review, "isso é bug ou não".

**Custo:** 4h. **Ganho:** feature de impacto para vender em readme/demo.

### 3.3 Skill marketplace
61 skills é muito. Não há `alpha skills install <name>` puxando de índice remoto, nem `alpha skills publish`. Resolve também a tensão "skills com credenciais não vão pro repo" do `ALPHA.md`.

**MVP simples:** índice JSON num gist/repo + `git clone` em `~/.alpha/skills/`. Sem servidor.

**Custo:** 2 dias. **Ganho:** ecossistema de skill autoria.

### 3.4 Replay determinístico de sessão
Salvar trace completo (prompts + tool calls + responses + seed) e fazer **replay** contra outro provider. Feature mais pedida em agentic frameworks, raramente entregue bem. `history.py` já tem persistência; falta `alpha replay <session-id> --provider anthropic --diff`.

**Custo:** 1 dia. **Ganho:** poderoso para debug, audit, comparação de providers.

---

## §4. Distribuição — gargalo invisível

Hoje rodar Alpha Code = `git clone` + venv + `pip install -e .` + `.env`. Para você é trivial. Para qualquer outra pessoa é fricção fatal.

### Tier 1 — PyPI: `pipx install alpha-code`
Já tem `pyproject.toml` e `[project.scripts]` apontando para `main:main`. **Bloqueador:** paths absolutos. `_PROJECT_ROOT = Path(__file__).parent.parent` quebra `prompts/` e `skills/` quando instalado em site-packages. Mover para `importlib.resources` + `package_data`.

**Custo:** 3–4h. **Ganho:** enorme — destrava qualquer adoção.

### Tier 2 — Binário standalone
`uv tool install alpha-code` ou PyInstaller. Resolve Windows (hoje recomenda WSL2). **Custo:** 1 dia após Tier 1.

### Tier 3 — Imagem Docker oficial
`docker run -v $(pwd):/workspace ghcr.io/freire19/alpha-code` para CI/sandbox. Resolve §1.3 automaticamente. **Custo:** 4h.

---

## §5. Observabilidade — voando às cegas

Hoje não há resposta para:
- **Quanto custou cada sessão em USD?** `llm.py` tem `usage` na resposta; agregar por sessão e expor em `/cost`.
- **Latência por etapa.** Quanto demora LLM vs tool execution vs compress? Sem isso, otimização é palpite.
- **Logs estruturados.** `logger.info` em texto livre não é grep-friendly. JSON Lines em `~/.alpha/logs/` permite `jq` posterior. Trivial: um `JsonFormatter` em `logging`.

Adicionar `/stats` mostrando: tokens (in/out), custo, iterações, tempo por iteração, taxa de aprovação manual. Insight gratuito para usuário **e** para o autor entender uso real.

**Custo:** 4h. **Ganho:** dado para todas as decisões futuras.

---

## §6. Roadmap em três horizontes

### Horizonte 1 — Próximas 2 semanas (destravar + visibilidade)
| # | Item | Estimativa | Bloqueia |
|---|------|-----------|----------|
| 1 | #001 — `urllib3>=2.7` + regenerar lockfile | 15min | – |
| 2 | #002 — gating por path em `write_file` | 30min | release public |
| 3 | Quebrar `display/core.py` em renderers | 6–8h | qualquer evolução visual |
| 4 | `/cost` + `/stats` + logs JSON | 4h | decisões data-driven |
| 5 | #011 — centralizar `safe_json_loads` | 30min | – |

### Horizonte 2 — Próximo mês (diferenciação)
| # | Item | Estimativa | Por quê |
|---|------|-----------|---------|
| 6 | Provider protocol + Gemini adapter como prova | 1 dia | abre 4o/5o provider barato |
| 7 | `delegate_consensus` + multi-agent no README | 4h | tira feature do limbo |
| 8 | Replay determinístico de sessão | 1 dia | feature única no nicho |
| 9 | Memory persistente cross-session | 1 dia | lock-in real |
| 10 | Snapshot test de `prompts/system.md` | 2h | trava regressão de prompt |

### Horizonte 3 — Próximo trimestre (distribuição)
| # | Item | Estimativa | Destrava |
|---|------|-----------|----------|
| 11 | PyPI release (resolver paths absolutos) | 1 dia | adoção externa |
| 12 | Sandbox opcional para destructive tools | 2 dias | CI / agentes longos |
| 13 | Skill registry + `alpha skills install` | 2 dias | ecossistema |
| 14 | Docker image oficial | 4h | sandbox + Windows |
| 15 | Binário standalone (PyInstaller/uv) | 1 dia | Windows nativo |

---

## §7. O que **não** fazer

- **Não** reescrever `agent.py` para LangGraph/CrewAI/qualquer framework. O loop atual está enxuto e correto.
- **Não** adicionar feature de UI antes de quebrar `display/core.py`. Vai virar bola de neve.
- **Não** fazer release PyPI antes do #002 (plant+execute). Issue exposta publicamente vira CVE.
- **Não** prometer multi-agent no README sem o `delegate_consensus` — feature mostrada sem coordinator decepciona.
- **Não** adicionar provider novo antes do `ProviderProtocol`. Hoje vira if/elif eterno.

---

## §8. Métricas de sucesso (como saber que subiu de nível)

| Estado atual | Meta H1 | Meta H2 | Meta H3 |
|--------------|---------|---------|---------|
| Instala: clone + venv + pip | Igual | Igual | `pipx install alpha-code` |
| Maior arquivo: 1162 linhas | < 600 | < 400 | < 400 |
| Coverage: ? | medido | > 60% | > 75% |
| Custo visível? Não | Sim (`/cost`) | Sim | Sim |
| Providers: 5 (via if/elif) | 5 | 6+ (via protocol) | 6+ |
| Multi-agent visível? Não | Não | Sim (README + consensus) | Sim |
| Replay? Não | Não | Sim | Sim |
| Memory persistente? Não | Não | Sim | Sim |
| Sandbox? Não | Não | Não | Sim (opt-in) |

---

*Documento vivo. Atualizar conforme decisões forem tomadas — especialmente a de §0 (pessoal vs produto), que reordena tudo abaixo.*
