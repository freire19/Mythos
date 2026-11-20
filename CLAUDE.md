# CLAUDE.md

Architecture and module-level orientation for Claude Code working in this
repo. **For other audiences:**

- **End users** → `docs/USER_GUIDE.md` (install, REPL commands, settings).
- **Public README** → `README.md`.
- **Behavior rules for the Alpha runtime itself** → `ALPHA.md` (auto-loaded
  by Alpha; covers what to do / not do when editing here).

This file deliberately avoids re-stating install/run instructions or
configuration syntax — those live in the docs above. What's here is the
internal architecture you need to navigate the codebase.

## Current status (updated 2026-11-02)

- Version **1.31.2** — single package `alpha-code`, release-please on master.
- Test suite: **2792 passed, 1 skipped** (`pytest tests/`, ~15s, no network).
- Nov 2026 "parity with Claude Code" wave, in progress — order: (1) thinking
  rendered in the UI ✅, (2) lazy tool loading ✅, (3) persistent memory read
  path ✅ (the write path already existed; the summary was never injected),
  (4) fork-with-context sub-agents ✅ (`delegate_task(inherit_context=true)`),
  (5) worktree isolation ✅ (`isolation="worktree"`), (6) resumable
  sub-agents ✅ (`agent_message` / `agent_list`), (7) in-REPL scheduling ✅
  (`/loop`), (8) per-function model routing ✅ (`provider:model` specs,
  `ALPHA_COMPRESS_PROVIDER` / `ALPHA_SUBAGENT_PROVIDER`), (9) MCP
  resources ✅ (`mcp_list_resources` / `mcp_read_resource`), (10)
  interactive MCP OAuth ✅ (`/mcp auth`). **Wave complete.** Follow-up
  (same day): finished **background** sub-agents are resumable too
  (`alpha/bg_resume.py`, transcript handoff via `ALPHA_BG_TRANSCRIPT`), and
  `tool_search` ranks by tool category + plural folding (seen in a real
  DeepSeek smoke: "database … tables" returned `browser_query` first); a
  *running* background child is messageable via its inbox
  (`ALPHA_BG_INBOX`); desktop notifications for long turns / approval
  prompts (`alpha/notify.py`, `ALPHA_NOTIFY`); local `/review` of the
  working tree / PR (via `gh`) / branch / commit / path (`alpha/review.py`).
- Audits: **all zeroed** — AUDIT_V2.3 plus the six per-category deep dives
  (security, bugs, logic, resilience, performance, maintainability). The
  reports live in `docs/audits/` and `docs/STATUS.md`, both gitignored, so
  they exist only in a local checkout. Only follow-up left is user-side on
  GitHub: a `pypi` environment with required reviewers gating the publish job.
- July 2026 feature wave, all merged: Anthropic **extended thinking**,
  `web_fetch`, `edit_file(replace_all)`, CLI `--continue`/`--resume`,
  **plan mode**, headless **`-p` + `alpha.sdk`**, sub-agent **profiles +
  background delegation**, **MCP Streamable HTTP**, hook events
  `session_start`/`pre_compact`/`subagent_stop` + secret-stripped hook env.
- Known gaps (P2 backlog): none open from the parity list. Candidates:
  cloud sessions, IDE extension, cloud multi-agent PR review. (Local
  remote control shipped: `alpha/remote.py`.)

## Architecture overview

The agent runs an async generator loop:

```
LLM call → tool detection → approval gate → tool execution → repeat
```

Max 100 iterations per turn (`LIMITS["max_iterations"]` in `config.py`),
1h wall-clock budget, optional session cost cap. Events are yielded to the
caller for real-time streaming display.

### Run modes / core flow

```
main.py
  run_repl()    interactive REPL — plan mode, queued inputs, /commands
  run_single()  positional one-shot with TTY approval prompts
  run_print()   headless -p — text|json|stream-json, deny-by-default,
                stdin piping, --max-turns; setup noise → stderr on JSON
    → alpha.sdk.run_task() (print mode) or _run_once() (REPL/single)
      → agent.run_agent() (async generator)
        → llm.stream_chat_with_tools()  (dispatch por api_format;
          Anthropic vai para llm_anthropic via providers registry)
        → executor.execute_tool_calls()
      ← yields {token, tool_call, tool_result, approval_needed, done, error}
```

`alpha.sdk.run_task` / `run_task_sync` is the public embedding API:
approval policy `deny`/`auto`/callable, `max_turns` chaining via the
`[CONTINUE: …]` marker (`sdk.extract_continue` is the canonical
implementation — `main.py` imports it).

### Module map

| Module | Responsibility |
|---|---|
| `alpha/agent/` | Package. `__init__.py` orchestrates the loop: context-compression trigger, iteration/wall-clock/cost caps, bg-task notification injection, pre-flight enforcement, forced final summary. `loop.py` holds the loop detectors. |
| `alpha/llm.py` | SSE streaming client for OpenAI-compatible APIs. Incremental tool-call accumulation, DSML/JSON tool-call recovery (DeepSeek/Ollama), retry with jittered backoff, loop-aware shared `httpx.AsyncClient`. |
| `alpha/llm_anthropic.py` | Anthropic `/v1/messages` adapter (`api_format: "anthropic"`). Prompt caching (ephemeral breakpoints on system/tools/prefix, `ALPHA_ANTHROPIC_CACHE=0` to disable) **and extended thinking**: `thinking` param auto-enabled for Claude 3.7+/4+ (`ALPHA_ANTHROPIC_THINKING`, budget `ALPHA_THINKING_BUDGET` added on top of `ALPHA_MAX_TOKENS`, temperature forced to 1). Thinking/redacted blocks round-trip verbatim via `thinking_blocks` on the assistant message — same contract as DeepSeek `reasoning_content`. Both adapters also emit `thinking_token` stream events (side-channel `state.pending_thinking`, never mixed into `content`); the agent loop republishes them as public `{"type": "thinking"}` events and the REPL renders them dim via `display.print_thinking` (`ALPHA_SHOW_THINKING=compact|full|off`, `config.show_thinking_mode`). |
| `alpha/executor.py` | Runs tool calls. Single calls sequential; multi-call batches fan out approved tools via `asyncio.gather`. Hard-blocks denied tools, runs `pre_tool` hooks (parallelized) with veto, fires `post_tool`. Timeouts: 120s default, 300s for `_SLOW_TOOLS`; truncates results > 12K chars. Results carrying `_image_path` (`browser_screenshot`, system `screenshot`, `android_screenshot`) are re-attached as a vision user message when the provider supports it; old screenshots pruned to the last 3. When the provider has NO vision (DeepSeek/Grok/Ollama), `_describe_for_blind_provider` has Gemini (`ocr.describe_images`, model `GEMINI_VISION_MODEL`, default `gemini-4-hydrogen`) describe the image and appends the text as a user note (`_image_question` on the result steers it; `ALPHA_VISION_FALLBACK=off` disables; tests force it off in `conftest.py`). `_USER_WAIT_TOOLS` (`ask_choice`) run without a timeout and credit the wait to the turn budget. `build_assistant_tool_message` carries `reasoning_content`/`thinking_blocks`. |
| `alpha/approval.py` | Two-tier safety: built-in defaults (auto-approve list, shell allowlist of 75+ commands with operator blacklist) plus user `allow`/`deny` rules from `.alpha/settings.json`. `is_denied(name, args)` is a hard-block (no prompt) and consults `plan_mode.deny_reason` first. `needs_approval` decides auto-execute vs prompt; "always allow" persists the narrowest scoped rule. Sensitive-path writes always prompt. |
| `alpha/plan_mode.py` | Plan-mode session state. Three enforcement layers: `filter_openai_tools` (model only sees SAFE + `present_plan`), `deny_reason` hard-block in `approval.is_denied` (fail-closed for unknown/MCP tools), and `PLAN_MODE_NOTE` prefixed to the user turn. Entered via `/plan` or Shift+Tab cycle; exited by an approved `present_plan` (REPL then queues "execute o plano") or `/plan off`. Mutually exclusive with auto-accept; `/clear` resets. |
| `alpha/sdk.py` | Public embedding API + engine of CLI print mode. `run_task`/`run_task_sync` over `run_agent`; approval `deny`/`auto`/callable; `max_turns` via `[CONTINUE:]`; `on_event` streaming callback; returns `TaskResult` (reply, events, turns, cost_usd). |
| `alpha/context.py` | Adaptive compression via LLM summarization (trigger ≥70% of provider context limit; protected tail 8→4→1; never splits tool_calls from results). Token estimate = chars/4 calibrated against real usage via EWMA. Hard-truncate fallback. `ContextVar` isolates parent vs sub-agent retry budgets. `/compact [foco]` (`cli/commands/compact_cmd.py`) runs `compress_context` on demand with `protected_tail=2` and passes the focus as `instructions` to `build_compression_prompt`. |
| `alpha/config.py` | Provider configs (`_PROVIDERS`: deepseek, openai, anthropic, google/gemini, antigravity, grok, ollama, gemma-*), feature flags (`FEATURES`), limits (`LIMITS`), tool timeouts/caps, system prompt loader. Tri-state getters: `browser_tools_enabled`, `apify_tools_enabled`, `vision_tool_results_enabled`, subagent policy. **Provider specs**: `get_provider_config` accepts `name:model` (model override); `base_provider()` is the key for provider-keyed tables (rate limit, context limit); `route_provider("compress"|"subagent", fallback)` reads `ALPHA_COMPRESS_PROVIDER` / `ALPHA_SUBAGENT_PROVIDER`. |
| `alpha/scheduler.py` | `/loop` registry (process-local, max 10, intervals 10s–24h). `pop_one_due` re-anchors on `now` (busy REPL fires once). Delivery lives in `repl_input.read_input`: a due prompt is returned instead of blocking; while blocked, `_arm_schedule_timer` runs a daemon `threading.Timer` that calls `app.exit(_SCHEDULE_SENTINEL)` via `app.loop.call_soon_threadsafe` only when the buffer is empty (re-checks every 5s otherwise). Command handler in `cli/commands/schedule_cmd.py`. |
| `alpha/agent_sessions.py` | Registry of resumable in-process sub-agents (`remember`/`get`/`acquire`/`release`/`list_sessions`/`reset`). Holds the finished run's `prep` whose `messages` list IS the live history (run_agent appends in place). Backs the `agent_message` / `agent_list` tools. |
| `alpha/bg_resume.py` | Resume of **background** sub-agents. The parent pre-assigns the `agent_id` and passes `ALPHA_BG_TRANSCRIPT=<ws>/.alpha/runs/bg/agents/<id>.json` to the `alpha -p` child; `cli.entry.run_print` dumps the final conversation there (system dropped, orphan tool_calls sanitized, tool outputs clipped, reasoning removed, 0600). `delegate_tools._revive_background` loads + validates it (roles, tool_call pairing, size caps), rebuilds a `prep` with the same id (`reuse_scratch=True`) and our sub-agent system prompt, and registers it in `agent_sessions`; `agent_list` merges disk transcripts not yet in memory (`source: "background"`). **Inbox** for a child still running: `ALPHA_BG_INBOX=<id>.inbox.jsonl`; `agent_message` → `post_message` (append-only JSON lines, `delegate_tools._bg_agent_tasks` maps id → bg task to tell running from finished); the child's `agent._drain_bg_inbox` drains it (atomic rename) at the top of every iteration AND at the no-tool-calls stop point (injects the reply + a user note, then `continue`s). Unread leftovers travel with the first resume message (`unread_inbox_delivered`). |
| `alpha/worktree.py` | Git worktree isolation for sub-agents: `create` (branch `alpha/<id>` under `.alpha/runs/worktrees/`), `changed_files` (ignores `.alpha/`), `finalize` (drop clean / keep + report dirty), `remove`. Async over `run_subprocess_safe`; errors are `WorktreeError`, surfaced as tool errors. |
| `alpha/tool_lazy.py` | Tool lazy-loading: deferred-tool classification, session-global loaded set, `apply()` (prompt-side filter + dynamic `tool_search` index) and `search()` (select:/keyword resolver). Registry entry lives in `tools/toolsearch_tools.py`. See "Lazy loading" under Tool system. |
| `alpha/notify.py` | Desktop notifications for the REPL: `turn_started`/`turn_finished` (called around the turn in `cli/entry.py`), `approval_needed` (before `print_approval_request` in `cli/setup.approval_callback`), `background_finished` (from `agent._drain_bg_notifications`). Backends `notify-send` → `osascript` → terminal bell, run in a daemon thread, shell-free, text clipped/sanitized. `ALPHA_NOTIFY=auto|on|off` (`config.notify_mode`; auto = TTY and not CI; delegated/headless children never notify), `ALPHA_NOTIFY_AFTER` seconds threshold (default 20) for turn-end/approval nudges. |
| `alpha/remote.py` / `remote_page.py` | **Remote control** (`alpha --remote [lan\|local]`, `/remote on [--local]\|off\|status` in `cli/commands/remote_cmd.py`). Stdlib `ThreadingHTTPServer` in a daemon thread (the REPL recreates its asyncio loop per turn) + `_Hub`: bounded event backlog with `seq`, SSE subscribers (`GET /api/events?since=`, read by the page with `fetch` so the token goes in a header), inbox, the open `Question`, busy flag. Auth: `Authorization: Bearer` on every `/api/*`, token only in the URL fragment; the page (`remote_page.PAGE`) is static, escaped-then-rendered Markdown, strict CSP. Bind = LAN address by default, loopback with `--local`; plain HTTP. REPL hooks, all no-ops while off: `repl_input.read_input` returns a pending message / `_arm_remote_waker` exits the idle prompt (`_REMOTE_SENTINEL`, never while the buffer has text); `cli/entry._run_once` feeds a `TurnMirror` (token → non-persistent `delta`, flushed `assistant`, `tool_call`/`tool_result` summaries, `turn_end`); `capture_output` tees slash-command output; `print_approval_request` and `ask_choice` call `remote.ask` and pass `question.event` as the menu's `cancel` — `MenuCancelled` then reads `question.answer`. `POST /api/interrupt` cancels the open menu first, then SIGINTs the process only while a turn is busy. `!cmd` is refused from the remote. |
| `alpha/review.py` | `/review` (handler in `cli/commands/review_cmd.py`). `resolve(target)` → `ReviewTarget`: no arg = working tree vs HEAD (+ untracked list), `N`/`#N`/PR URL = `gh pr view --json` + `gh pr diff`, branch = `git diff <base>...<branch>` (base `--base` → `origin/HEAD` → main/master), commit-ish = `git show`, existing path = `git diff HEAD -- path` (untracked shown via `--no-index`). All subprocess argv (no shell), safe env copy, 60s timeout; diff control/bidi-stripped and clipped at `ALPHA_REVIEW_MAX_CHARS` (default 60K, floor 2K) on a line boundary. `build_prompt` labels the diff read-only data, asks for verdict + severity-ordered `path:line` findings, honours `--fix` (edits still gated) and `--comment` (PR only, `gh pr comment`). `--deep` = local multi-agent review: `save_diff` writes the material to `.alpha/runs/review/<ts>-<label>.diff` (0600) and the prompt tells the model to call `delegate_parallel` once with one task per `DEEP_DIMENSIONS` entry (correctness/security/contracts, each reads the file with `read_file`), then verify every candidate against the code before the final report; falls back to a plain review when delegation is disabled. History records the slash line, not the prompt. Headless: `run_print` calls `review_cmd.expand_headless` so `alpha -p "/review <t> [--deep]"` works in CI (exit 2 = bad target, 0 + stderr note = nothing to review); `.github/workflows/pr-review.yml` posts the reply as one PR comment (`--edit-last`) when `ALPHA_REVIEW_API_KEY` is set. |
| `alpha/project_context.py` | Walk-up resolver for `ALPHA.md`. 16 KB cap. Opt-out via `ALPHA_NO_PROJECT_CONTEXT=1`. |
| `alpha/memory.py` | Cross-session memory. Write path: `record_memory` tool (`tools/memory_tools.py`) → `~/.alpha/memory/{global,workspace-<dir>-<hash>}.md`, 8 KB cap per file, cross-process flock. Read path: `inject_memory` appends a `# MEMORY` section as the LAST layer of the system prompt (`cli/setup.build_system_prompt` and `sdk.run_task` default prompt), 4 000-char cap, `ALPHA_NO_MEMORY=1` to skip. `/memory list|forget|clear|edit` in `cli/commands/memory_cmd.py`. |
| `alpha/settings.py` | Shared loader for `.alpha/<file>` config files. `find_config_file(name)` resolves CWD → project root → home. |
| `alpha/hooks.py` | Declarative hooks from `.alpha/settings.json`. Events: `pre_tool` (veto via `blocking: true`), `post_tool`, `on_user_prompt`, `on_stop` (atexit in `cli/lifecycle.py`), `session_start` (fired in `_setup_agent_session`), `pre_compact` (in `agent._maybe_compress`), `subagent_stop` (in `delegate _run_subagent`). Payload via stdin JSON + `ALPHA_*` env vars. Secret-named env vars are stripped from hook env by default (#D118); per-hook `pass_secrets: true` opts in. |
| `alpha/mcp/` | MCP clients. Stdio (`client.py`): sync `subprocess.Popen` + reader threads. Streamable HTTP (`http_client.py`): sync httpx, `Mcp-Session-Id` sessions, JSON or SSE responses, https required outside loopback, auth via static `headers` in mcp.json (`${VAR}` expansion; secrets refused in URLs) **or OAuth 2.1** (`oauth.py`: discovery via `WWW-Authenticate`/well-known, dynamic client registration, PKCE + loopback callback, token store `~/.alpha/mcp_tokens.json` 0600 with silent refresh). Without a static Authorization header a 401 raises `MCPAuthRequired`; the interactive flow runs only from `/mcp auth <server>` (REPL foreground, then `loader.connect_server`), never inside the 15s startup handshake or headless. Both sync because the CLI recreates the event loop per turn; `loader.py` treats them by duck-typing (`AnyMCPClient`) and registers tools as `mcp__<server>__<tool>` (DESTRUCTIVE by default). `initialize` stores server `capabilities`; the mixin also speaks `resources/list` + `resources/read`, surfaced by the SAFE built-ins `mcp_list_resources` / `mcp_read_resource` (`tools/mcp_resource_tools.py`, via `loader.get_client`). |
| `alpha/display/` | Package: ANSI terminal UI. `core.py` re-exports; approval prompts + auto-accept state live in `renderers/prompts.py`; `markdown.py` renders streamed replies; `thinking.py` spinner. Transcript layout: `●` at column 0 for assistant prose (`format_assistant_text`, wrapped by `markdown.wrap_ansi`) and tool headers (`● Read(rel/path)`, display names in `renderers/tools._DISPLAY_TOOL_NAME_ALIASES`), results hang as `  ⎿  …` (bulk tools collapse to `Read N lines` via `_bulk_summary`; lines are trimmed with `theme.fit`, never terminal-wrapped), submitted input is erased and re-printed by `format_user_echo`. Parallel batches: `_TurnState` defers every header but the first so each result prints under its own. Scroll region: `_setup_scroll` makes room with relative moves (newlines + cursor-up) and `_teardown_scroll` restores the cursor — an absolute restore can land BELOW the region, where output overwrites itself on the last row. Approval prompts in Portuguese; special cards for `present_plan`, `pre_flight`, `todo_write`. |
| `alpha/cli/` | REPL glue. `commands/` = slash-command dispatch (`_DISPATCH`: sessions, `/plan`, `/accept-edits`, `/rewind`, `/cost`, `/memory`, `/review`, `/compact`, `/export`, `/doctor`, …; each new command = one `*_cmd.py` module + one `_DESCRIPTIONS` line, the catalogue test enforces it). `setup.py` = session setup (`_setup_agent_session` fires `session_start`) + `approval_callback` (disables plan mode on approved `present_plan`). `lifecycle.py` = shutdown (on_stop hook, browser/MCP teardown). `skills_cli.py` = `alpha skills install/list/update/remove`. |
| `alpha/repl_input.py` / `repl_concurrent.py` | `prompt_toolkit` editor: image clipboard paste (Ctrl+V), slash autocomplete, **Shift+Tab cycles normal → accept-edits → plan**, bottom toolbar shows the active mode, `/loop` delivery (see `scheduler.py`). `repl_concurrent` queues input typed while the agent runs. `!<cmd>` is handled in `cli/entry.run_repl` before slash dispatch via `alpha/bang.py` (user shell, no approval/sandbox, clipped transcript of the last 5 prepended to the next LLM message via `drain_context`; history keeps the raw input; `ALPHA_BANG_TIMEOUT`). |
| `alpha/history.py` | Session persistence (`~/.alpha_code/history/`). Path-traversal guard; orphan tool_call sanitization on load. Backs `/save`, `/load`, `/continue` and the CLI flags `--continue`/`--resume` (REPL, single-shot and print). |
| `alpha/checkpoints.py` | File snapshots before every mutation (write/edit/SNR) + turn markers. `/rewind` restores (last turn / N edits). Does NOT undo shell side effects. Kill-switch `ALPHA_CHECKPOINTS=0`. |
| `alpha/bg_tasks.py` | Background process registry: `execute_shell(background=true)` AND `delegate_task(background=true)` children. Detached process, log in `.alpha/runs/bg/`, `bg_tasks_list`/`bg_task_output`/`bg_task_kill` tools, finish notification injected into the next agent iteration. |
| `alpha/cost.py` / `stats.py` / `jsonlogs.py` | `/cost` (token spend + USD by provider/model, cache-aware pricing), `/stats`, opt-in JSON-lines logs (`ALPHA_JSON_LOGS=1`). Session cost cap `ALPHA_MAX_SESSION_COST_USD` enforced in the agent loop. |
| `alpha/sandbox.py` | Optional firejail/bubblewrap wrapper for destructive shell + `execute_python` (`.alpha/settings.json` `sandbox` block or `ALPHA_SANDBOX=1`; fail-closed when enabled but backend missing). |
| `alpha/skills/` | Skill registry (auto-discovers `*/SKILL.md` under `<repo>/skills/` and `~/.alpha/skills/`; 60+ bundled). Installer from git/local paths. |
| `alpha/agents/` | Named-agent profiles (`agent.yaml`): provider/model/temperature/workspace/system_prompt_extra + tool/skill allow/deny filters. Used by the REPL (`/agent`, `@nome msg`) and by `delegate_task(agent=...)`. Workspace path enforcement in `workspace.py`. |

## Tool system

Tools are `ToolDefinition` dataclasses (`alpha/tools/__init__.py`):

- `name`, `description`, JSON schema parameters.
- `safety`: `SAFE` (auto-run) or `DESTRUCTIVE` (gate by `needs_approval`).
- `executor`: async callable.
- `category`: `ToolCategory` enum or string literal.

**~76 built-in tools across 20 `*_tools.py` modules**, including 6 `android_*` (`android_tools.py`: test an app on a real phone over adb — `android_devices`/`android_screenshot`/`android_ui_dump`/`android_logcat` are SAFE, `android_input` and `android_app` are DESTRUCTIVE and blocked in sub-agents; no generic `adb shell`, no uninstall/clear-data; deferred category `android`; rules of engagement live in `skills/android-testing`) and 31
`browser_*` (Playwright, persistent session across turns, or **attached to the user's own Chrome** over CDP via `ALPHA_BROWSER_CDP` (env-only opt-in, loopback unless `ALPHA_BROWSER_CDP_ALLOW_REMOTE=1`; the agent opens and tracks ONLY its own tabs via `BrowserSession.adopt_page` — no context-wide "page" listener, user tabs never guarded/read/closed; close = close own tabs + disconnect); coordinate
computer-use) — whole families are dropped from the prompt via tri-state
flags (`ALPHA_BROWSER_TOOLS`, `ALPHA_APIFY_TOOLS`). Notable singles:
`web_search` (DuckDuckGo + trafilatura), `web_fetch` (single URL,
SSRF-guarded), `edit_file` (exact-string; ambiguity errors unless
`replace_all=true`), `screenshot`/`browser_screenshot` (feed vision via
`_image_path`).

**Registration**: `load_all_tools()` auto-discovers `*_tools.py` under
`alpha/tools/` plus optional `plugins/`. MCP tools are appended *after*
the built-ins so a misbehaving server can't shadow native tools.

**Dispatch**: `get_openai_tools()` returns the registry in OpenAI
function-calling format (accepts a `name_filter`). `get_tool(name)`
retrieves a `ToolDefinition`.

**Lazy loading (`alpha/tool_lazy.py`)**: `run_agent` calls
`tool_lazy.apply(tools, get_tool_fn)` before EVERY LLM call. Deferred
tools (categories browser/scraping/database, a few rare singles, MCP when
> `ALPHA_LAZY_MCP_THRESHOLD`) are dropped from the prompt and indexed in
the description of a dynamic `tool_search` entry (SAFE, auto-approved).
`search()` marks hits as loaded in a session-global set (reset by
`/clear`) so they ride along from the next iteration. Invariants: `apply`
never mutates or widens its input (profile/plan-mode/sub-agent filters
still bound what can appear) and dispatch is lenient — an unloaded
deferred tool called directly still executes through the normal gates.
`get_openai_tools()` never advertises the static `tool_search` registry
entry. Kill switch `ALPHA_LAZY_TOOLS=off`. Prompt-size effect: 69 → 31
schemas, ~9.4K → ~6.7K tokens with no MCP servers.

### Composite tools (`alpha/tools/composite_tools.py`)

`project_overview`, `run_tests`, `search_and_replace`, `deploy_check`
are macro-tools that orchestrate sub-tools via `_run_tool`. **Trust
model (#D110)**: `_run_tool` skips the approval gate — the composite
itself was already approved (deny rules ARE re-checked). Composites that
touch state are marked `DESTRUCTIVE`.

## Planning tools & plan mode

- `present_plan(summary, steps)` — `DESTRUCTIVE`, gates execution behind
  user approval. Used at the start of any task with 3+ distinct steps.
- `pre_flight` — plan card before destructive batches (enforced by a
  re-prompt in the agent loop when 2+ destructive tools arrive without it).
- `todo_write(todos)` — `SAFE`, replaces (not appends) the full todo list.

Plan mode (see `alpha/plan_mode.py` row above) turns the session
read-only until a `present_plan` is approved — three independent
enforcement layers, entered via `/plan` or the Shift+Tab cycle.

## Multi-agent delegation

`multi_agent_enabled` and `delegate_tool_enabled` are ON by default:

- `delegate_task` — one focused sub-agent. `agent=<name>` applies a named
  profile (provider/temperature/prompt extra/tool filters — a profile can
  only RESTRICT beyond the safety blocklist, never widen it).
  `background=true` spawns a detached headless child (`alpha -p`,
  deny-by-default approvals) through `bg_tasks` — survives the turn, logs
  under `.alpha/runs/bg/`, notifies on finish. The child gets
  `ALPHA_DELEGATED=1`, which blocks ALL delegation inside it
  (anti-fork-bomb guard in `_check_delegation_enabled`).
  `inherit_context=true` **forks**: the executor publishes the live
  conversation in the `parent_messages` ContextVar (`tools/_delegate_core.py`)
  and `build_transcript` flattens it into ONE sanitized read-only text block
  (system msgs dropped, tool outputs labelled as data + clipped to 1.5K,
  24K total keeping the tail, control/bidi stripped) prepended to the
  sub-agent's task. Never replayed as structured messages. Not available
  with `background=true`.
  `isolation="worktree"` runs the sub-agent in its own `git worktree`
  (`alpha/worktree.py`): branch `alpha/<agent-id>` from HEAD under
  `.alpha/runs/worktrees/<agent-id>` (gitignored), workspace boundary =
  that dir. Clean worktree → removed with its branch; dirty → kept and
  reported in `result["worktree"]` (path, branch, changed_files, hint).
  Never merged automatically. Not with `background=true`.
- `delegate_parallel` — up to `max_parallel_agents` (default 3), total cap
  10 per call. Also accepts `inherit_context` and `isolation` (one
  worktree per task — parallel editors can't collide).
- `agent_message(agent_id, message)` — **resume** a finished in-process
  sub-agent with its full history, same tool filters/blocklist/workspace
  and a fresh budget (`alpha/agent_sessions.py`: bounded registry of
  `prep` objects, `MAX_SESSIONS=8`, reset by `/clear`). `agent_list` (SAFE)
  shows resumable ones. Requires approval; blocked inside sub-agents
  (`DELEGATION_TOOLS` in `_delegate_core.py`). Worktree runs are not
  resumable; **finished background runs are** (`alpha/bg_resume.py`: the
  child's transcript is revived in-process, result carries
  `resumed_from: "background"`), and a background run **still running**
  gets the message through its inbox (`status: "queued"`; the child reads
  it at its next iteration or before stopping).
- `delegate_consensus` — N agents answer the same question; majority +
  dissent via similarity clustering.

In-process sub-agents: 15 iterations / 120s budget (`ALPHA_SUBAGENT_MAX_SECONDS`, floor 30), isolated scratch dir
at `{ws}/.alpha/runs/{agent-id}/`, no parent conversation context, and —
without an approval callback — `SUBAGENT_DESTRUCTIVE_BLOCKLIST` applies
(shell/python/http/browser-write/etc blocked; write_file/edit_file remain
auto-approved by policy; git read-only only). `delegate_*` recursion is
always blocked.

## Loop detection (`alpha/agent/loop.py`)

Five detectors, gated after 6 calls:

1. Exact repetition — same call signature ≥ 3×.
2. Similar calls — same tool + fuzzy-matched args (≥ 0.92) ≥ 5×.
3. Cycle — `A→B→A→B` over a 20-call window.
4. Stale progress — recent tool results all near-identical.
5. Error streak — ≥ 4 consecutive failures.

On detection the agent appends a synthetic user note and forces a final
response without tools. Exception: the FIRST error-streak per task only
injects a corrective nudge and continues WITH tools (grep-without-matches
is legitimate work); the second streak takes the terminal path.

## CI

`.github/workflows/ci.yml` runs `pytest` on Python 3.11–3.14 plus a `docker-smoke` job (build + `--help` + import) on every
PR and push to master (browser tests gated by Playwright availability).
`release.yml` = release-please (`RELEASE_PLEASE_PAT` with `GITHUB_TOKEN`
fallback). `pr-review.yml` = Alpha reviews same-repo PRs with
`alpha -p "/review <n> --deep"` and posts one comment; no-op without the
`ALPHA_REVIEW_API_KEY` secret (forks/Dependabot never get secrets). Full suite is fast (~8s) and network-free — run it after any
change: `.venv/bin/python -m pytest tests/ -q`.
