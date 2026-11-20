# Plano-Browser — Alpha no nível "controlar a web como o Claude"

> Criado em 2026-07-16. Baseado na análise de gap entre o subsistema atual
> (`alpha/tools/browser_*.py`, 21 tools Playwright) e o Claude in Chrome /
> computer-use. Complementa o `Plano-Upgrade-v3.md` (gaps gerais de harness).

## Diagnóstico resumido

O que já existe e está sólido:

- 21 tools Playwright (`_browser_registrations.py`): sessão, navegação,
  histórico, 4 tools de abas, screenshot, get_content, describe_page,
  query, wait_for, click/fill/select por seletor CSS, press_key, execute_js.
- Segurança de URL fail-closed (schemes bloqueados, SSRF via
  `net_utils.validate_url`, guard `framenavigated` pós-redirect,
  allowlist/blocklist por env).
- Playwright como dependência opcional (`[browser]` extra) com degradação
  graciosa e gating tri-state (`browser_tools_enabled()`).

Os 5 gaps, em ordem de impacto:

| # | Gap | Efeito |
|---|-----|--------|
| 1 | Modelo não vê screenshots (recebe só o path) | Agente navega "cego", só por texto/DOM |
| 2 | Sessão morre a cada turno + browser isolado sem logins | Não dá pra operar fluxos autenticados |
| 3 | Sem click por coordenada / scroll / type / combos | Sem computer-use real; só CSS selector |
| 4 | Sem console, network, accessibility tree | Não debuga webapps |
| 5 | Sem upload, download, GIF, wait-for-navigation | Fluxos incompletos |

---

## Etapa 1 — Screenshots multimodais (CONCLUÍDA em 2026-07-16)

**Objetivo:** o resultado de `browser_screenshot` chega ao LLM como imagem
real quando o provider ativo suporta vision — o modelo passa a VER a página.

### Design

O formato "no fio" das mensagens é sempre OpenAI; o adapter
(`llm_anthropic.py`) converte para o shape Anthropic. A injeção usa esse
mesmo pipeline que já existe para imagens coladas pelo usuário
(`attachments.build_user_content`).

1. **Convenção de transporte** — a tool retorna a chave `_image_path` no
   result dict. Chaves com `_` já são ocultadas do JSON textual enviado ao
   LLM (`executor._format_result`), então o path não duplica no texto.
2. **Injeção no executor** — `execute_tool_calls()` ganha parâmetro
   `provider`. Após registrar as tool messages do batch, resultados com
   `_image_path` geram UMA mensagem `user` adicional por tool call:
   `[{type: text, nota explicando a origem}, {type: image_url, data URL}]`.
   A mensagem vai DEPOIS de todas as tool messages do batch — inserir no
   meio quebraria a validação OpenAI (toda tool_call_id precisa de resposta
   imediata) e fragmentaria o coalescing de tool_result do adapter Anthropic.
3. **Gate por provider** — `config.vision_tool_results_enabled(provider)`:
   env `ALPHA_VISION_TOOL_RESULTS` tri-state (`auto` default / `1` força /
   `0` desliga); em `auto`, liga sse `supports_vision` do provider ativo.
   DeepSeek (default) não recebe imagem — nada muda para quem não usa
   provider vision.
4. **Adapter Anthropic** — `_convert_messages` passa a fazer merge de
   mensagens `user` consecutivas (tool_results primeiro, depois text/image
   no mesmo turno user), garantindo alternância de roles válida.
5. **Guardas** — reusa `attachments.MAX_IMAGE_BYTES` (5 MB): imagem
   ilegível/grande demais → injeção é pulada com warning em log, tool
   message textual permanece intacta.

### Arquivos tocados

- `alpha/tools/browser_tools.py` — `_browser_screenshot` retorna `_image_path`.
- `alpha/config.py` — `vision_tool_results_enabled(provider)`.
- `alpha/executor.py` — param `provider`, coleta e injeção pós-batch.
- `alpha/agent/__init__.py` — `run_agent` repassa `provider` ao executor.
- `alpha/llm_anthropic.py` — merge de user messages consecutivas.
- `tests/test_vision_tool_results.py` — novo.

### Critérios de aceite

- [x] Com provider vision + `_image_path` no result → messages contém a
      mensagem user com bloco `image_url` após as tool messages do batch.
- [x] Com provider sem vision (deepseek) → nenhuma injeção; wire idêntico
      ao atual.
- [x] `ALPHA_VISION_TOOL_RESULTS=0` desliga mesmo com provider vision.
- [x] Batch paralelo: injeções vêm depois de TODAS as tool messages.
- [x] Adapter Anthropic: `tool → user(imagem)` vira um único turno user com
      `tool_result` primeiro e `image` depois (merge de user turns
      consecutivos em `_convert_messages`).
- [x] Imagem > 5 MB ou inexistente → sem crash, sem mensagem user vazia.
- [x] Suíte verde: 1510 passed, 1 skip. Única falha é pré-existente e
      ambiental (`test_delegate_parallel_requires_approval` — o
      `.alpha/settings.json` do repo tem `delegate_parallel` na allowlist,
      e o teste lê as regras locais; falha também na árvore limpa).

Cobertura nova: `tests/test_vision_tool_results.py` (19 testes: coleta de
`_image_path`, injeção sequencial/paralela, gates de provider/env, merge
no adapter, screenshot expõe `_image_path` + viewport).

### Limitações conhecidas (aceitas nesta etapa)

- Base64 infla o histórico da sessão (`history.py`) e o contexto —
  `context.py` já contabiliza custo por imagem e anota imagens na
  compressão, mas sessões com muitos screenshots crescem rápido.
  Follow-up na Etapa 1.1.
- Vale para qualquer tool que retorne `_image_path` no futuro (o mecanismo
  é genérico), mas nesta etapa só `browser_screenshot` emite.

### Etapa 1.1 (follow-up curto) — CONCLUÍDA em 2026-07-16 (parcial)

- [x] Poda de imagens antigas: só as N últimas imagens de tool vivem no
  contexto (default 3, `ALPHA_VISION_KEEP_IMAGES`; <=0 desativa). Imagens
  coladas pelo USUÁRIO nunca são podadas — só as marcadas com o prefixo
  `[ALPHA SYSTEM NOTE] Image produced by tool`. Implementado no executor
  (`_prune_old_vision_messages`), não no context.py: roda no momento do
  append, antes de a imagem sequer entrar no histórico.
- [ ] Downscale opcional (Pillow, se instalado) para ~1568px de largura máx —
  reduz tokens sem perder legibilidade. (Pendente — baixa prioridade.)

---

## Etapa 2 — Sessão persistente + perfil com login (CONCLUÍDA em 2026-07-16)

**Objetivo:** sessão de browser sobrevive entre turnos e mantém logins.

Implementação (opção a):

1. [x] **Loop dedicado** — `_BrowserLoop` (browser_session.py): event loop
   numa thread daemon lazy-started; `run_on_browser_loop` decora TODOS os
   executors browser_* na registração (`_reg`). Os objetos Playwright nunca
   trocam de loop → a mesma sessão (cookies, abas, login) vive a conversa
   inteira. Reentrante (chamada aninhada executa inline). O mecanismo
   loop-aware antigo permanece como defesa em profundidade.
2. [x] **Perfil persistente** — `browser_open(profile=true)` ou
   `ALPHA_BROWSER_PROFILE=1`: `launch_persistent_context` com
   `user_data_dir` de `ALPHA_BROWSER_PROFILE_DIR` (default
   `~/.alpha/browser-profile`, chmod 700). Sem user_agent custom nesse modo
   (login flows precisam parecer browser normal). Cookies/logins sobrevivem
   entre sessões do Alpha.
3. [x] **Troca de modo a quente** — `browser_open` com profile/headless
   diferente fecha e reconstrói a sessão (`_close_locked`, lock não
   reentrante); status `reopened`.
4. [x] **Shutdown correto** — `shutdown_browser()` despacha o close para o
   loop do browser e para a thread; contexto persistente é fechado
   explicitamente (grava o perfil).
5. [x] **Segurança** — chmod 700 no perfil; warning quando perfil ativo sem
   `ALPHA_BROWSER_ALLOWLIST`.

Fluxo de login: `browser_open(profile=true, headless=false)` → usuário loga
na janela → sessões seguintes reusam o perfil já autenticado.

**Fora de escopo (decidido):** `connect_over_cdp` no Chrome real do usuário —
paridade total com Claude in Chrome, mas exige Chrome com
`--remote-debugging-port`, compartilha estado com a navegação pessoal e
amplia demais a superfície de risco. Reavaliar depois da Etapa 5.

## Etapa 3 — Interação computer-use (CONCLUÍDA em 2026-07-16)

Novas tools (todas DESTRUCTIVE exceto scroll):

- `browser_click_at(x, y, button="left", click_count=1)` — `page.mouse.click`.
  Coordenadas são as do viewport do último screenshot (documentar no
  description da tool).
- `browser_scroll(direction, amount_px | to_selector)` — `mouse.wheel` /
  `scroll_into_view_if_needed` (SAFE).
- `browser_type(text, delay_ms=20)` — `keyboard.type` char a char (para
  inputs que rejeitam `fill`, ex. editores rich-text).
- `browser_press_combo(keys)` — ex. `Control+A`, `Control+Shift+R`
  (Playwright já aceita combo string em `keyboard.press`; estender
  `browser_press_key` em vez de criar tool nova se ficar natural).
- `browser_hover(selector | x,y)` — menus que abrem on-hover.
- `browser_drag(from, to)` — `mouse.down/move/up`.

Sinergia com Etapa 1: screenshot → modelo enxerga → clica por coordenada.
Incluir dimensões do viewport no resultado do screenshot para o modelo
calibrar coordenadas.

Implementadas: `browser_click_at` (coordenada, double-click, botão),
`browser_scroll` (direção/px ou scroll-into-view), `browser_type` (char a
char, para rich-text), `browser_hover` (seletor ou coordenada),
`browser_drag`, e `browser_press_key` documentado para combos
(Control+A etc. — o Playwright já aceitava). Interações novas entram na
SUBAGENT_DESTRUCTIVE_BLOCKLIST como as equivalentes por seletor.

## Etapa 4 — Observabilidade da página (CONCLUÍDA em 2026-07-16)

- `browser_read_console(pattern?, level?, limit=50)` — listener
  `page.on("console")` + ring buffer por página (instalado no `open()`),
  com filtro regex como o Claude in Chrome.
- `browser_read_network(url_pattern?, status?, limit=50)` — listeners
  `request/response` + ring buffer (método, URL, status, content-type,
  tamanho; sem bodies por padrão).
- `browser_aria_snapshot(selector?)` — `page.locator(...).aria_snapshot()`
  (Playwright ≥ 1.44): árvore de acessibilidade real em YAML, substitui o
  `describe_page` heurístico como visão estrutural primária (manter
  `describe_page` pelos seletores prontos).
- Ring buffers com cap (ex. 500 entradas) para não vazar memória em
  sessões longas.

Implementadas: ring buffers (500 entradas) de console e network na sessão
(`_wire_observability`, ligado em toda página), `browser_read_console`
(regex + level), `browser_read_network` (regex de URL + status + falhas),
`browser_aria_snapshot` (árvore de acessibilidade YAML via locator;
requer Playwright >= 1.49, com erro amigável em versões antigas).

## Etapa 5 — Complementos (PARCIAL em 2026-07-16)

- [x] `browser_upload(selector, path)` — `set_input_files`; path validado
  DENTRO do workspace (mesmo guard do screenshot #D105-SEC).
- [x] `browser_wait_for_navigation(url_pattern?, state, timeout)` —
  `wait_for_url` / `wait_for_load_state`.
- [ ] Downloads: `accept_downloads=True` com diretório fixo no workspace +
  tool `browser_wait_download`. (Pendente — muda um default de segurança,
  merece decisão própria.)
- [ ] Gravação: `record_video_dir` do Playwright no `browser_open(record=true)`
  → `browser_stop_recording` retorna o `.webm` (conversão GIF opcional via
  ffmpeg se presente). (Pendente — exige recriar o context.)

---

## Gaps gerais (fora do browser — ordem sugerida pós-Etapa 2)

Detalhados no `Plano-Upgrade-v3.md`; registrados aqui só para priorização:

1. ~~**Checkpoints/rewind de arquivos**~~ ✅ (2026-07-16) —
   `alpha/checkpoints.py`: snapshot antes de write_file/edit_file/
   search_and_replace, marcador por turno, `/rewind` (turno | N | list),
   cap com poda, kill-switch `ALPHA_CHECKPOINTS=0`.
2. ~~**Task manager de background**~~ ✅ (2026-07-16) — `alpha/bg_tasks.py`:
   `execute_shell(background=true)` → task_id + log em `.alpha/runs/bg/`;
   tools `bg_tasks_list`/`bg_task_output`/`bg_task_kill`; notificação
   `[ALPHA SYSTEM NOTE]` injetada no turno seguinte ao término; `/tasks`
   no REPL. Sub-agentes não recebem notificações nem podem matar tasks.
3. **ProviderProtocol** — desacoplar o loop do formato OpenAI (já iniciado:
   `alpha/providers/` + `_register("anthropic", ...)`).
4. **LSP/diagnostics** — pyright/tsserver via stdio para go-to-definition
   e erros estruturados.
5. **Pipeline multimodal unificado** (`alpha/media/`) e publicação PyPI.

Também corrigidos em 2026-07-16 (achados do audit em produção):
- **Error streak não mata mais o turno na 1ª detecção** — vira nudge com
  tools disponíveis (auditorias com grep/pytest falhando eram abortadas);
  reincidência ainda força a resposta final.
- **Truncagem de tool results head+tail** (Issue #005) — o final do campo
  (onde erros vivem) é preservado, com marcador explícito de omissão.

## Sequência de execução

| Ordem | Etapa | Tamanho | Status |
|-------|-------|---------|--------|
| 1 | Etapa 1 — screenshots multimodais | M | ✅ concluída (2026-07-16) |
| 2 | Etapa 1.1 — poda/downscale de imagens | P | pendente |
| 3 | Etapa 2 — sessão persistente + perfil | M/G | ✅ concluída (2026-07-16) |
| 4 | Etapa 3 — computer-use (coordenadas) | M | ✅ concluída (2026-07-16) |
| 5 | Etapa 4 — console/network/aria | M | ✅ concluída (2026-07-16) |
| 6 | Etapa 5 — upload/waits (✅) · downloads/GIF (pendente) | P/M | parcial (2026-07-16) |
