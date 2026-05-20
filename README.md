# Mythos

Autonomous cybersecurity agent that connects to multiple LLM providers (DeepSeek, OpenAI, Anthropic, Grok, Ollama). Specialized in vulnerability research, code auditing, exploit development, and system hardening.

Pure async Python, minimal dependencies (`httpx`, `python-dotenv`, `ddgs`, `pyyaml`, `prompt_toolkit`).

## Features

- **Multi-provider** — switch between DeepSeek, OpenAI, Anthropic, Grok, Ollama via env or `--provider` flag.
- **Adaptive context compression** — multi-pass LLM-driven summarization with shrinking protected tail; auto-recovers from context-window overflow.
- **Image attachments** — paste screenshots directly in the REPL (`Ctrl+V`/`Alt+V`) or attach via `/image <path>`.
- **PDF & audio attachments** — `/pdf <path>` extracts text via `pypdf`; `/audio <path>` transcribes via OpenAI Whisper. Works with any provider since both inject as text.
- **MCP support** — connect external Model Context Protocol servers via `.alpha/mcp.json`.
- **Hooks & permissions** — declarative `pre_tool` / `post_tool` / `on_user_prompt` / `on_stop` hooks plus per-tool `allow`/`deny` rules in `.alpha/settings.json`.
- **Multi-agent delegation** — `delegate_task`, `delegate_parallel`, and `delegate_consensus` (N agents answer the same question, returns majority + dissent) for fanning out focused sub-agents with isolated workspaces.
- **Session observability** — `/cost` shows running token spend and estimated USD by provider/model; `/stats` reports iterations, tool latency, and approval rate. Opt-in JSON-lines logs via `ALPHA_JSON_LOGS=1` write to `~/.alpha/logs/`.
- **Plan & todos** — built-in `present_plan` (gates execution behind approval) and `todo_write` tools.

## Install

Requires Python ≥ 3.11.

```bash
git clone <your-repo-url>/Alpha_Mythos.git
cd Alpha_Mythos
```

Then follow the section for your OS.

### Linux / WSL

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env  # then fill in your API keys
```

For image clipboard paste, install `xclip` (X11) or `wl-clipboard` (Wayland):

```bash
sudo apt install xclip          # Debian/Ubuntu (X11)
sudo apt install wl-clipboard   # Debian/Ubuntu (Wayland)
```

### macOS

```bash
brew install python@3.11        # if you don't already have 3.11+
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Image clipboard paste works out of the box (uses native `pbpaste`).

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env
```

If `Activate.ps1` is blocked, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

For image clipboard paste, the recommended path is **WSL2 + xclip** — native PowerShell handles text fine, but the image-grab path in `alpha/clipboard.py` is tested against Linux/macOS clipboard tools.

### Optional extras (all OSes)

```bash
pip install -e ".[browser]"      # adds Playwright tools
pip install -e ".[multimodal]"   # adds pypdf for /pdf attachments
pip install -e ".[exploit]"      # adds pwntools + docker for binary exploitation
pip install -e ".[dev]"          # adds pytest
```

### Docker (no host install)

A two-stage Dockerfile builds a wheel and installs it into a slim
runtime image. Useful for CI, throwaway sandboxes, and Windows users
who'd rather not touch WSL2.

```bash
docker build -t mythos .
docker run --rm -it \
  -v "$PWD:/workspace" \
  -e DEEPSEEK_API_KEY \
  mythos "what does this repo do?"
```

The mounted workspace is where Mythos reads/writes; anything outside
the mount is ephemeral. The image runs as a non-root user.

### Standalone binary (no Python at runtime)

Build a single-file executable that ships its own Python interpreter,
so end users can run `mythos` without installing Python or pipx at all.
Useful for Windows users, locked-down environments, or distribution
via GitHub Releases.

```bash
pip install -e ".[binary]"
pyinstaller alpha.spec
./dist/mythos --help
```

The resulting `dist/mythos` is ~20MB on Linux (~30MB compressed with
`upx=True` in `alpha.spec`). It bundles the prompts and core deps
but excludes the optional extras (`browser`, `multimodal`, `exploit`,
etc.) to stay lean — users needing those install from source instead.

## Update

Easiest path — the bundled updater handles pull + reinstall + diff of `.env.example`:

```bash
# Linux / macOS / WSL / Git Bash
mythos-update              # jumps to latest master
mythos-update v2.1.0       # pins to a specific release tag
```

```powershell
# Windows (PowerShell, native — no bash needed)
.\bin\alpha-update.ps1
.\bin\alpha-update.ps1 v2.1.0
```

Restart the REPL afterwards (`/exit` then `mythos` again).

Manual equivalent if you prefer:

```bash
cd Alpha_Mythos
git pull origin master
source .venv/bin/activate         # Linux/macOS
# .\.venv\Scripts\Activate.ps1    # Windows
pip install -e . --upgrade
```

If `.env.example` gained new variables, run `diff .env.example .env` to spot what to copy over.

To pin to a specific release tag instead of tracking `master`:

```bash
git fetch --tags
git checkout v2.1.0          # any tag from `git tag -l "v*"`
pip install -e . --upgrade
```

Run `git checkout master && git pull` to return to the rolling latest.

## Run

```bash
# Interactive REPL
python main.py

# Single-shot task
python main.py "your task"

# Provider override
python main.py --provider anthropic "your task"
python main.py --list-providers

# Wrapper that activates the venv automatically
./bin/mythos "your task"
```

## Configuration

| File | Purpose |
|------|---------|
| `.env` | API keys per provider, default provider, workspace root |
| `.alpha/settings.json` | Permission rules (`allow`/`deny`), hooks |
| `.alpha/mcp.json` | MCP server definitions |
| `agents/<name>/agent.yaml` | Named agent profiles (model, tools, workspace) |
| `alpha/prompts/system.md` | Top-level agent system prompt (bundled with the package) |

`.example` templates ship in `.alpha/` and `.env.example`.

## REPL commands

```
/help        Show command help
/tools       List available tools
/mcp         List connected MCP servers
/agents      List named agents
/agent       Switch active agent
/model       Switch provider/model
/image PATH  Attach an image (Ctrl+V also works)
/pdf PATH    Attach a PDF (text extracted via pypdf)
/audio PATH  Attach audio (transcribed via OpenAI Whisper)
/clear       Clear conversation history
/sessions    List saved sessions
/cost        Token spend + estimated USD this session
/stats       Iterations, tool latency, approval rate
/preflight   Lifetime feedback on pre_flight cards (approve/reject mix)
/sandbox     Print the active shell-sandbox state
```

## Sandbox (optional, Linux)

For higher-risk red-team operations on untrusted code, opt destructive
shell commands AND `execute_python` into a `firejail` or `bubblewrap`
sandbox via `.alpha/settings.json`:

```json
{
  "sandbox": {
    "enabled": true,
    "tool": "auto",
    "deny_network": true,
    "extra_args": []
  }
}
```

`tool` accepts `auto` (firejail preferred, bubblewrap fallback),
`firejail`, or `bubblewrap`. With `deny_network: true` the sandboxed
command can't reach the network — useful for blast-radius reduction on
untrusted exploit code, expected to break installers and curl-based
fetches. Install the backend you want: `sudo apt install firejail` (or
`bubblewrap`). The `/sandbox` REPL command prints the active state.

Override per-session without editing settings: `ALPHA_SANDBOX=1 mythos ...`.

> The shell-sandbox above (`alpha/shell_sandbox.py`) wraps individual
> shell tools. Mythos also ships a separate exploit-execution sandbox
> (`alpha/sandbox.py`, Docker/podman containers) used by the red-team
> tools (`run_exploit`, `sandbox_test`) — that one is enabled
> automatically for exploit workflows and is not configured here.

## Managing skills

Install, list, update, and remove skills from git repos or local paths:

```bash
mythos skills install github:user/repo          # GitHub shorthand
mythos skills install https://example.com/r.git # any git URL
mythos skills install /path/to/local/skill      # local directory
mythos skills install ./my-skills --force       # overwrite existing
mythos skills list                              # show installed + sources
mythos skills update                            # re-fetch every tracked skill
mythos skills update myskill                    # update just one
mythos skills remove myskill
```

Multi-skill repos (each subdir under `skills/` with a `SKILL.md`) install
every entry. Skills land in `~/.alpha/skills/` and the agent auto-discovers
them on next launch.

## Architecture & internals

See [`CLAUDE.md`](./CLAUDE.md) for the agent loop, tool registration, MCP integration, hook payloads, and module layout.

## License

[MIT](./LICENSE)
