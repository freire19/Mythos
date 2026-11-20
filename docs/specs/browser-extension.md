# RFC: Extensão de navegador — Alpha na sidebar

**Status:** draft (nada implementado)
**Author:** Claude (Opus 4.8) + Jonatas Freire
**Created:** 2026-07-22
**Baseline:** v1.31.1 · suíte 1673/1673

---

## Problema

O Alpha é um agente de terminal. Quando ele precisa de navegador, sobe um
Chromium isolado via Playwright (`alpha/tools/browser_tools.py`, 31 tools).
Esse Chromium **não é o navegador do usuário** — não tem os cookies, os
logins, as sessões, as extensões dele. Toda tarefa que depende de estar
autenticado em algum lugar exige refazer o login dentro de uma instância
descartável.

Ao mesmo tempo, o valor de uma sidebar tipo ChatGPT for Chrome / Claude AI
é justamente o contrário: ela vive no navegador real, mas é só um chat —
não tem filesystem, não tem shell, não tem MCP, não tem as 70 tools.

A oportunidade é a interseção: **um agente na sidebar do navegador real do
usuário que também mexe no código e no shell da máquina dele.** Nenhuma das
extensões existentes faz isso, porque nenhuma delas tem um agente de
terminal por trás.

---

## Estado atual do código (verificado em 2026-07-22)

Levantamento feito antes de propor qualquer coisa:

| Pergunta | Resposta |
|---|---|
| Existe servidor HTTP/WS no repo? | **Não.** Zero ocorrências de `websocket`, `aiohttp.web`, `uvicorn`, `fastapi`, `asyncio.start_server` em `alpha/` e `tests/`. |
| `alpha/mcp/http_client.py` serve? | Não — é **cliente** MCP Streamable HTTP, direção oposta. |
| Existe API programática? | Sim, `alpha/sdk.py` — `run_task`/`run_task_sync`, já com `on_event` streaming e `TaskResult`. É a fundação. |
| Dependência de servidor disponível? | `aiohttp>=3.9,<4.0` já está no extra `[network]` do `pyproject.toml`. `aiohttp.web` dá WebSocket **sem dependência nova**. |

Conclusão: o SDK já expõe exatamente o que uma UI remota precisa. O que
falta é transporte.

---

## Arquiteturas consideradas

### A — Extensão fina + Alpha local ✅ recomendada

```
┌─────────────────────────┐         ┌──────────────────────────┐
│ Chrome (real, do user)  │         │ Máquina local            │
│                         │         │                          │
│  Side Panel (chat UI)   │◄──WS───►│  alpha serve             │
│         │               │ 127.0.0.1│  └─ alpha/server.py      │
│         │               │  :8765  │      └─ sdk.run_task()    │
│  Content script ────────┼─────────┼──►   └─ agent loop        │
│  (lê/clica na aba ativa)│ bridge  │          ├─ 70 tools      │
│                         │ inverso │          ├─ MCP, skills   │
└─────────────────────────┘         │          └─ shell/fs      │
                                    └──────────────────────────┘
```

- **Ganho:** reusa 100% do agente. Tools, MCP, skills, delegation,
  checkpoints, plan mode, gate de aprovação, histórico — nada é reescrito.
- **Custo:** o usuário precisa ter o Alpha instalado e rodando `alpha serve`.
  Não é uma extensão que se instala e usa.

### B — Extensão autônoma chamando a API do provider

Service worker em TS chama Anthropic/DeepSeek direto. Zero setup,
publicável na Web Store sem ressalvas.

**Rejeitada:** exige reescrever o agent loop, o executor, o approval gate e
as tools em TypeScript — e mesmo assim ficaria sem filesystem e sem shell,
que é o motivo de existir do Alpha. Entregaria "mais uma sidebar de chat"
depois de semanas de trabalho.

### C — Modelo local no browser (WebLLM/WebGPU)

**Rejeitada:** modelos ≤8B rodando em WebGPU têm tool-calling não confiável.
Um agente cujo loop depende de tool-calling correto não funciona nesse
regime. Serve para demo, não para o produto.

**Decisão: A.** As fases abaixo detalham A.

---

## Componente 1 — `alpha/server.py` (novo)

Servidor local sobre `aiohttp.web`, exposto pelo comando `alpha serve`.

### Rotas

| Rota | Método | Função |
|---|---|---|
| `/health` | GET | versão, provider ativo, se há sessão em andamento |
| `/chat` | WS | canal principal — bidirecional, ver protocolo abaixo |
| `/sessions` | GET | lista de `alpha/history.py` para o seletor da sidebar |

### Protocolo do WebSocket

**Cliente → servidor:**
```jsonc
{ "type": "prompt",   "text": "...", "session_id": "..." }
{ "type": "approve",  "id": "req-7", "approved": true }
{ "type": "tool_result", "id": "call-3", "result": {...} }  // bridge inverso
{ "type": "cancel" }
```

**Servidor → cliente** — reaproveita os eventos que o agent loop já emite
(`alpha/agent/__init__.py` faz `yield` de `token`, `tool_call`,
`tool_result`, `error`, `anomaly`, `done`), repassados via `on_event` do
`run_task`. Só dois tipos são novos:
```jsonc
{ "type": "approval_needed", "id": "req-7", "tool": "...", "args": {...} }
{ "type": "browser_call",    "id": "call-3", "tool": "page_read", "args": {...} }
```

### Detalhe não-óbvio: o approval callback é síncrono

`alpha/sdk.py:102` tipa `approval` como
`str | Callable[[str, dict], bool]` — **síncrono**. Não dá para `await` a
resposta do browser dentro dele.

Mas `alpha/executor.py:743` e `:864` invocam o callback via
`await asyncio.to_thread(approval_callback, tool_name, args)`. Ou seja:
**ele já roda numa worker thread**, não na thread do event loop. Isso torna
o round-trip trivial e seguro:

```python
def approval_cb(tool_name: str, args: dict) -> bool:
    # roda numa worker thread (executor.py:743) — pode bloquear
    fut = asyncio.run_coroutine_threadsafe(
        ask_browser(ws, tool_name, args), loop
    )
    return fut.result(timeout=APPROVAL_TIMEOUT)
```

Sem essa observação a tendência seria refatorar o executor para approval
async — trabalho grande e desnecessário.

### Detalhe não-óbvio: o loop de eventos

O `CLAUDE.md` registra que os clientes MCP são síncronos "porque a CLI
recria o event loop a cada turno". No servidor **isso não acontece** — o
loop é único e vive enquanto o processo vive. É um ambiente mais amigável
que o REPL, não menos. Mas vale confirmar que os clientes MCP síncronos
não bloqueiam o loop: eles usam `subprocess.Popen` + threads e `httpx`
sync, então as chamadas precisam ir para `asyncio.to_thread` se hoje não
forem.

---

## Componente 2 — extensão MV3 (novo diretório `extension/`)

```
extension/
  manifest.json        # MV3
  sidepanel/           # UI de chat (HTML/TS)
  background.js        # service worker
  content.js           # ponte com a aba ativa
```

`manifest.json`, essencial:
```jsonc
{
  "manifest_version": 3,
  "permissions": ["sidePanel", "storage", "tabs", "scripting"],
  "host_permissions": ["http://127.0.0.1:8765/*"],
  "side_panel": { "default_path": "sidepanel/index.html" }
}
```

### Detalhe não-óbvio: o service worker morre

No MV3 o service worker é encerrado após ~30s de ociosidade. **Ele não pode
ser o dono do WebSocket** — a conexão cairia no meio de uma tarefa longa,
que é exatamente o caso de uso aqui.

Solução: o **side panel** abre e mantém o socket (documentos de side panel
têm vida longa enquanto o painel está aberto), com reconexão automática e
replay do último `session_id` para retomar o contexto. O service worker
fica só com o que precisa de permissão elevada (`chrome.scripting`).

### UI

Streaming de tokens, render de markdown, cards de aprovação (o mesmo
conteúdo que `alpha/display/renderers/prompts.py` desenha em ANSI, agora em
HTML), indicador de custo (`alpha/cost.py` já calcula), seletor de sessão.

---

## Componente 3 — bridge inverso de tools

É a peça que justifica o projeto e a de maior risco.

Quando o agente decide inspecionar a página, em vez de subir o Playwright,
o servidor emite `browser_call` pelo WS; a extensão executa na aba real e
devolve `tool_result`. O agente nunca sabe a diferença.

Implementação: um conjunto novo de `ToolDefinition`s registradas via
`register_tool` (`alpha/tools/__init__.py:94`) apenas quando o servidor
está ativo e há extensão conectada — `page_read`, `page_click`,
`page_fill`, `page_screenshot`, `tabs_list`. O executor delas não chama
Playwright: faz o round-trip pelo WS.

Quando não há extensão conectada, elas não são registradas e o agente cai
nas `browser_*` do Playwright normalmente. Sem flag, sem branch no prompt.

**Riscos:** timeout (a aba pode estar num modal, ou o usuário navegou para
fora no meio); a aba alvo pode fechar entre a chamada e a execução;
content scripts não injetam em `chrome://` nem na Web Store; e o agente
passa a agir numa sessão **autenticada** do usuário — o que é o valor e
também o perigo. Ver segurança abaixo.

---

## Modelo de segurança (bloqueante — não é polimento)

Um servidor em localhost que executa shell é uma superfície de ataque
séria. Qualquer página aberta no navegador pode fazer `fetch` para
`127.0.0.1:8765`. Sem as defesas abaixo, **visitar um site malicioso vira
execução de código na máquina**.

Requisitos, todos obrigatórios na Fase 1:

1. **Bind apenas em `127.0.0.1`.** Nunca `0.0.0.0`. Sem flag para mudar.
2. **Token compartilhado.** Gerado em `~/.alpha_code/server_token` com
   permissão `0600`, exigido em toda conexão. A extensão o recebe via
   colagem única do usuário no primeiro uso.
3. **Validação de `Origin`.** Aceitar somente `chrome-extension://<id>`
   conhecido. Rejeitar toda origem `http(s)://` — nenhuma página web tem
   motivo legítimo para falar com o servidor.
4. **Aprovação continua valendo.** O canal remoto não pode implicar
   `approval="auto"`. O default é o mesmo do REPL; tool destrutiva gera
   card na sidebar e espera o clique.
5. **Sem `execute_shell` implícito no bridge.** As tools do bridge inverso
   operam na aba, não na máquina.

Isso também é o que a Chrome Web Store vai querer ver documentado se a
extensão for publicada — a Google implica com extensão que conversa com
localhost, e com razão.

---

## Fases

| Fase | Entrega | Verificável por |
|---|---|---|
| **1** | `alpha/server.py` + `alpha serve` + auth (token, origin, bind) | testes de integração: handshake, token errado → 401, origin web → 403 |
| **2** | WS `/chat` com streaming + approval round-trip | `websocat`/script Python fala com o servidor sem extensão nenhuma |
| **3** | Extensão MV3 mínima: side panel, conecta, faz streaming, aprova | uso manual em dev mode (unpacked) |
| **4** | Bridge inverso (`page_read`, `page_click`, …) | tarefa real numa aba autenticada |
| **5** | Sessões (`/sessions`, retomar), custo na UI, polimento | — |

Fases 1 e 2 têm valor independente: um servidor local do Alpha serve
qualquer cliente — extensão, app mobile, web UI, editor. Se a extensão for
abandonada, esse trabalho não é perdido.

---

## Testes

O repo tem 1673 testes e a suíte é offline e roda em ~8s. Isso precisa
continuar valendo:

- Servidor: `aiohttp.test_utils` sobe o app em porta efêmera, sem rede
  externa. Cobrir auth (token/origin/bind), streaming, approval
  round-trip, cancelamento, reconexão.
- Bridge inverso: WS falso do lado do cliente; nenhum navegador real.
- Extensão: fora do pytest. Vitest para a lógica de protocolo, teste manual
  para a UI. Não entra no CI de Python.

---

## Questões em aberto

1. **Autenticação da extensão.** Colar o token manualmente é feio. `alpha
   serve` poderia abrir uma página de pareamento local, mas isso reintroduz
   uma origem `http://` — precisa de desenho cuidadoso.
2. **Múltiplos clientes.** Duas janelas do Chrome com o painel aberto na
   mesma sessão: rejeitar a segunda, ou espelhar? Rejeitar na Fase 3.
3. **Qual workspace?** O REPL usa o CWD. `alpha serve` roda de onde? Provável:
   seletor de projeto na sidebar, com o servidor restrito a uma allowlist de
   diretórios definida em `.alpha/settings.json`.
4. **Publicação.** Web Store custa US$5 + review, e uma extensão que fala com
   localhost atrai escrutínio. Dev mode (unpacked) resolve o uso pessoal e
   não bloqueia nada.

---

## Estimativa

Fases 1–3 (servidor + sidebar funcional com streaming e aprovação): ~2 dias.
Fase 4 (bridge inverso) é a mais cara e a de mais incerteza — os modos de
falha só aparecem em uso real.
