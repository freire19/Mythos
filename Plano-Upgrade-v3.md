# Plano de Upgrade — Alpha Code (v3)

> **Síntese crítica de `Plano-Upgrade.md` (v1) + `Plano-Upgrade2.md` (v2).**
> Mantém a espinha estratégica da v1 (decisão pessoal-vs-produto, threat model,
> distribuição, "não fazer") e absorve da v2 as duas peças que faltavam:
> sistema de record/replay de LLM (§2.4.1) e pipeline multimodal (§3.5).
> Reposiciona multimodal **depois** de PyPI — sem distribuição, suporte a vídeo
> e PDF beneficia uma pessoa só.

---

## Mudanças desta versão

| Seção | Origem | Status |
|---|---|---|
| §0–§5 (estrutura) | v1 | mantida |
| §2.4.1 Record/replay LLM | v2 §1.1 | importada, com gancho na §2.4 |
| §3.5 Pipeline multimodal | v2 §3.1 | importada, **reposicionada** após §4 Tier 1 |
| §6 Roadmap | v1 | atualizado com itens novos |
| §7 Não fazer | v1 | +1 regra de priorização |
| §9 O que ficou de fora | novo | declara explicitamente o que da v2 foi descartado e por quê |

---

## Diagnóstico em uma linha

Alpha Code já é um Claude Code self-hosted bem-feito (17k LOC, 7k em testes, 6 audits zerados de CRÍTICO/ALTO, 5 providers, MCP, multi-agent). Subir de nível **não é consertar** — é escolher direção. Este plano cobre três frentes: **arquitetura sustentável**, **diferenciação real**, **distribuição**.

---

## §0. Decisão estratégica que precede tudo

**Alpha Code é ferramenta pessoal polida ou produto para outras pessoas?**

- Os audits dizem "produto" (esse rigor não se justifica para uso solo).
- Mas a falta de PyPI, README magro de features avançadas e invisibilidade de multi-agent dizem "pessoal".

Resolver isso **antes** de executar o plano. Se for pessoal, foco em §3 (diferenciação que melhora *seu* fluxo). Se for produto, §4 e §5 antes de §3.

**Esta versão do plano assume "produto"** — é a única hipótese em que multimodal e marketplace fazem sentido. Se for pessoal, corte §3.3, §3.5 e §4 inteira.

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
- **Snapshot do system prompt.** `prompts/system.md` é o ativo mais crítico e o menos testado. Snapshot test com `syrupy` ou similar — qualquer edit no system prompt aparece como diff explícito no PR. Custa 30 min e impede regressões silenciosas de identidade/comportamento.
- **Canário multi-provider.** Mesma sequência mockada nos 5 adapters → histórico final equivalente. Pega bug de adapter cedo. **Depende de §2.4.1.**

### 2.4.1 Sistema de record/replay de LLM (importado da v2 §1.1)

**Problema:** `tests/integration/` está quebrado — precisa de provider LLM real. CI não funcional para a suite completa. Sem isso, o canário multi-provider em §2.4 não é viável.

**Solução:** gravação/replay de respostas LLM em fixtures versionadas:

```
tests/fixtures/llm/
  deepseek/
    happy_path.json
    context_overflow.json
    tool_hallucination.json
    loop_repeat.json
  anthropic/
    loop_detection.json
    thinking_block.json
  openai/
    parallel_tool_calls.json
```

- Modo `record`: captura streaming real → serializa eventos (`token`, `tool_call`, `final`, `usage`) em JSON
- Modo `replay`: `FakeLLMClient` lê fixture e dá yield dos eventos na ordem
- Teste determinístico, rápido, sem API key
- **Crítico:** capturar 3 variações por cenário (normal, lento com pausas, com erro 5xx no meio) — sem isso, replay não pega bugs de streaming

**Entregáveis:**
- [ ] `tests/fixtures/llm/` com 8–10 cenários gravados, ≥3 variações cada
- [ ] `FakeLLMClient` como drop-in de `stream_chat_with_tools`
- [ ] `tests/integration/` rodando no CI sem rede
- [ ] Hook `pytest --record` pra regravar fixtures quando provider muda contrato

**Esforço:** 6h (não 4h como a v2 estimou — o trabalho de capturar variações de erro foi subestimado).
**Ganho:** destrava §2.4 inteiro + segurança pra refactors de `agent/`, `llm.py`, `context.py`.

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

Reaproveita a infra de §2.4.1 (mesmo formato de fixture) — uma fixture é um replay sintético, uma sessão real é um replay capturado.

**Custo:** 1 dia (4h se §2.4.1 já foi feito). **Ganho:** poderoso para debug, audit, comparação de providers.

### 3.5 Pipeline multimodal unificado (importado da v2 §3.1 — REPOSICIONADO)

**Importante:** este item depende de §4 Tier 1 (PyPI). Suporte a vídeo/áudio/PDF beneficia uma pessoa só se a única forma de instalar é `git clone + pip -e`. Multimodal antes de distribuição é vaidade.

**Problema:** Cada tipo de attachment tem caminho separado no código. OCR é separado do paste de imagem. Sem suporte para áudio, PDF, vídeo.

**Solução:** `alpha/media/pipeline.py` — chain de processadores:

```
attachment (bytes + MIME)
  → detector (magic bytes → MIME)
  → converter (PDF → texto+layout, áudio → transcrição, vídeo → frames + descrições)
  → chunker (divide texto longo respeitando contexto do provider)
  → injector (adiciona como user_content no formato multimodal da API)
```

| MIME | Processador | Biblioteca |
|------|-------------|------------|
| `image/*` | OCR (Gemini/local) | já implementado |
| `application/pdf` | extração texto+layout | `pdfplumber` |
| `audio/*` | transcrição | OpenAI Whisper API / `faster-whisper` local |
| `video/*` | frame sampling + descrição | `opencv-python` + Gemini vision |

**Entregáveis:**
- [ ] `alpha/media/` — `pipeline.py`, `detector.py`, `processors/{pdf,audio,video}.py`
- [ ] Comando `/attach arquivo.pdf` no REPL
- [ ] Chunking que respeita context window do provider ativo
- [ ] Drag & drop no terminal (best-effort, depende do emulador)

**Esforço:** 12h apenas para o pipeline + processadores. Whisper local adiciona +4h. Video confiável é +1 semana — **adiar pra fase posterior**, começar só com PDF + áudio.
**Ganho:** PDFs, transcrição de reuniões. **Não justifica** vir antes de PyPI.

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

**Sobre OpenTelemetry (proposto na v2):** adiar. OTel é correto para serviço multi-tenant; para uma CLI single-process, JSON logs + agregação local em `/stats` cobre 90% do valor com 10% do esforço. Adicionar OTel só quando aparecer caso real de export pra Grafana/Honeycomb.

---

## §6. Roadmap em três horizontes

### Horizonte 1 — Próximas 2 semanas (destravar + visibilidade)
| # | Item | Estimativa | Bloqueia |
|---|------|-----------|----------|
| 1 | #001 — `urllib3>=2.7` + regenerar lockfile | 15min | – |
| 2 | #002 — gating por path em `write_file` | 30min | release público |
| 3 | Quebrar `display/core.py` em renderers | 6–8h | qualquer evolução visual |
| 4 | `/cost` + `/stats` + logs JSON | 4h | decisões data-driven |
| 5 | #011 — centralizar `safe_json_loads` | 30min | – |
| 6 | **Record/replay LLM (§2.4.1)** | 6h | canário multi-provider, §3.4 replay |

### Horizonte 2 — Próximo mês (diferenciação)
| # | Item | Estimativa | Por quê |
|---|------|-----------|---------|
| 7 | Provider protocol + Gemini adapter como prova | 1 dia | abre 4o/5o provider barato |
| 8 | `delegate_consensus` + multi-agent no README | 4h | tira feature do limbo |
| 9 | Replay determinístico de sessão | 4h (com #6 pronto) | feature única no nicho |
| 10 | Memory persistente cross-session | 1 dia | lock-in real |
| 11 | Snapshot test de `prompts/system.md` | 30min | trava regressão de prompt |
| 12 | pytest-cov no CI (threshold 60%) | 1h | qualidade mensurável |

### Horizonte 3 — Próximo trimestre (distribuição + escala)
| # | Item | Estimativa | Destrava |
|---|------|-----------|----------|
| 13 | **PyPI release (resolver `_PROJECT_ROOT`)** | 1 dia | adoção externa, §3.5 |
| 14 | Sandbox opcional para destructive tools | 2 dias | CI / agentes longos |
| 15 | Docker image oficial | 4h | sandbox + Windows |
| 16 | Skill registry + `alpha skills install` | 2 dias | ecossistema |
| 17 | Binário standalone (PyInstaller/uv) | 1 dia | Windows nativo |
| 18 | **Multimodal: PDF + áudio (§3.5 fase 1)** | 12h | casos de uso novos |

### Horizonte 4 — Após adoção real (não estime, valide demanda)
- Video multimodal (caro, valor incerto sem usuários pedindo)
- OpenTelemetry / dashboards remotos (só se houver deploy multi-tenant)
- Plugin sandbox via subprocess (só se houver plugin de terceiro malicioso real)

---

## §7. O que **não** fazer

- **Não** reescrever `agent.py` para LangGraph/CrewAI/qualquer framework. O loop atual está enxuto e correto.
- **Não** adicionar feature de UI antes de quebrar `display/core.py`. Vai virar bola de neve.
- **Não** fazer release PyPI antes do #002 (plant+execute). Issue exposta publicamente vira CVE.
- **Não** prometer multi-agent no README sem o `delegate_consensus` — feature mostrada sem coordinator decepciona.
- **Não** adicionar provider novo antes do `ProviderProtocol`. Hoje vira if/elif eterno.
- **Não** suportar vídeo/áudio/PDF antes de PyPI. Beneficia uma pessoa só. (Nova regra v3.)
- **Não** adotar OpenTelemetry antes de validar que JSON logs + `/stats` são insuficientes.

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
| Tipos de mídia | Texto + imagem | Texto + imagem | Texto + imagem | + PDF + áudio (H3) |
| CI tests sem rede? Quebrado | Funcional (replay) | Funcional | Funcional |
| Snapshot do system prompt | Não | Sim | Sim | Sim |

---

## §9. O que ficou de fora (e por quê)

Itens da v2 que **não entraram** nesta síntese, com justificativa:

| Item v2 | Decisão | Motivo |
|---|---|---|
| §2.1 OpenTelemetry completo | **Cortado**, ver §5 v3 | Over-engineering para CLI single-process. JSON logs + `/stats` cobrem o caso. Reavaliar quando houver deploy multi-tenant. |
| §2.3 Middleware chain | **Cortado** | Hooks declarativos em `.alpha/settings.json` já fazem isso. Adicionar abstração interna sem caso de uso real é YAGNI. |
| §4.1 Plugin system com YAML + sandbox subprocess | **Adiado pra H4** | Não há plugin de terceiro hoje. Resolver problema que não existe = dívida cosmética. Manter o `_discover_plugins` atual até aparecer demanda. |
| §4.2 Multi-agent review loop | **Substituído por §3.2** | `delegate_consensus` da v1 cobre o caso de "agentes conversam pra melhorar output" com 1/3 do esforço (4h vs 10h). |
| §4.3 Reorganização de testes | **Cortado** | 6h de churn pra benefício marginal. Organização por batch é estranha mas funcional. Recursos melhor gastos em coverage (§2.4) e snapshot (§2.4 H2). |
| §3.5 Multimodal video | **Mantido mas adiado para H4** | Video confiável é semanas de trabalho com valor incerto. Começar por PDF + áudio (12h em H3) e validar demanda antes de investir em video. |
| Cronograma de 12 semanas / 74h | **Cortado** | Estimativa agregada de trabalho exploratório é fantasia. Substituído por horizontes com dependência declarada. |

---

## §10. Riscos reais (não os de cronograma)

| Risco | Sinal | Mitigação |
|---|---|---|
| §0 ficar indecidido | Plano executa 30% em todas as direções | Bloquear H2 até §0 ter resposta escrita em 1 parágrafo |
| `_PROJECT_ROOT` ter mais site-effects que `prompts/` e `skills/` | Quebra inesperada em PyPI | Grep `Path(__file__)` antes do Tier 1, listar tudo |
| Record/replay não capturar streaming flaky | Bugs de streaming passam no CI mas quebram em prod | Capturar 3 variações por cenário (normal/lento/erro 5xx) |
| Memory cross-session vazar contexto entre projetos | Skill aprende fato do projeto A e cita no projeto B | Memory escopada por workspace dir como chave primária |
| `delegate_consensus` virar "média de opiniões medíocres" | Output pior que sub-agente single | Rubrica clara + retornar disagreement explícito quando não há maioria |
| Multimodal explodir custo (Whisper API + frame extraction) | Sessão de 30 min queima $10 | `ALPHA_MAX_COST_PER_TASK` precisa existir **antes** do §3.5 (não no §3.5) |

---

*Documento vivo. Atualizar conforme decisões forem tomadas — especialmente a de §0 (pessoal vs produto), que reordena tudo abaixo. Próxima revisão sugerida: após primeiros 3 itens de H1 concluídos, pra validar se as estimativas batem.*
