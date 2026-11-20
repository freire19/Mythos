"""Declarative hook system for Mythos.

Hooks let users run shell commands at well-defined points in the agent
lifecycle without modifying the codebase. Configured in `.alpha/settings.json`.

## Schema

```json
{
  "hooks": {
    "pre_tool": [
      {
        "matcher": "write_file|edit_file",
        "command": "echo 'about to write' >> /tmp/audit.log",
        "blocking": true
      }
    ],
    "post_tool": [
      {"matcher": "write_file", "command": "ruff check \"$(printf '%s' \"$ALPHA_TOOL_ARGS_JSON\" | python -c 'import json,sys;print(json.load(sys.stdin).get(\"path\",\"\"))')\""}
    ],
    "on_user_prompt": [
      {"command": "echo \"$ALPHA_USER_PROMPT\" >> /tmp/prompts.log"}
    ],
    "on_stop": [
      {"command": "notify-send 'Alpha done'"}
    ]
  }
}
```

## Events

- `pre_tool`  — fires before each tool call. If a `blocking` hook exits
  non-zero, the tool is denied and the LLM sees the hook's stderr as
  feedback.
- `post_tool` — fires after each tool call (regardless of result). Output
  is logged but does not affect execution.
- `on_user_prompt` — fires when the user submits a message in the REPL.
- `on_stop` — fires once per session at shutdown.
- `session_start` — fires once when a session boots (REPL, single-shot or
  print mode), after provider/tools setup. Payload: provider, workspace.
- `pre_compact` — fires right before the context compressor rewrites the
  conversation. Payload: tokens_before, messages_count. Non-blocking —
  useful for snapshotting history before it's summarized away.
- `subagent_stop` — fires when a delegated sub-agent finishes (success or
  error). Payload: agent_id, status, tools_used.

## Variables passed to hook commands

Hook commands are run VERBATIM by the shell — there is NO `{path}`-style
template substitution (AUDIT_V2.2 #034). Read arguments from stdin (the full
event JSON) or from the env vars below.

The full event JSON is piped to the hook on stdin. The following env vars
are also exported for convenience:

- `ALPHA_HOOK_EVENT` — event name (`pre_tool`, etc.)
- `ALPHA_TOOL_NAME` — tool being called (pre/post_tool only)
- `ALPHA_TOOL_ARGS_JSON` — JSON-encoded args (pre/post_tool only)
- `ALPHA_USER_PROMPT` — user's input text (on_user_prompt only)
- `ALPHA_WORKSPACE` — active workspace path (if any)

## Security note — secret env vars are stripped by default

DEEP_SECURITY V3.0 #D118 (fechado): hooks NÃO recebem mais o `os.environ`
cru. Vars cujo NOME indica credencial (`*_API_KEY`, `*_TOKEN`, `*_SECRET`,
`*_KEY`, `*_PAT`, `*_DSN`, `DATABASE_URL`/`REDIS_URL`/`MONGO_URI`, …) ou
cujo VALOR é inequivocamente uma credencial (URL com `user:senha@`, chave
PEM) são removidas do env do hook (#101: heurística compartilhada em
`alpha/secret_env.py`) — um hook descuidado com `curl -d "$(env)"` não
vaza mais as chaves de provider nem connection strings.
PATH/HOME e o restante do ambiente passam normalmente.

Hooks que legitimamente precisam de um secret podem opt-in por entrada com
`"pass_secrets": true` — nesse caso trate o comando como você trataria um
`.bashrc`: só cole de fontes confiáveis.

## Matcher

A regex applied to the tool name (pre/post_tool only). If omitted or empty,
the hook fires for every tool. No anchoring — `write` matches both
`write_file` and `rewrite`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .secret_env import is_secret_env
from .settings import (
    JSON_INVALID,
    find_config_file,
    is_user_scoped_config,
    project_config_trusted,
    read_json_or_invalid,
)

logger = logging.getLogger(__name__)

VALID_EVENTS = (
    "pre_tool", "post_tool", "on_user_prompt", "on_stop",
    "session_start", "pre_compact", "subagent_stop",
)
HOOK_TIMEOUT = 30  # seconds

# #D118 + #101: vars cuja combinação NOME/VALOR sugere credencial são
# removidas do env dos hooks (mesma heurística usada no mcp/config para
# args/url) — `MY_PROJECT_DIR` passa; `OPENAI_API_KEY`, `GITHUB_TOKEN`,
# `DATABASE_URL` e `MINHA_VAR=postgres://u:senha@h/db` não.


def _hook_env(pass_secrets: bool) -> dict[str, str]:
    """Env base do hook: os.environ sem vars de credencial (default)."""
    if pass_secrets:
        return dict(os.environ)
    return {
        k: v for k, v in os.environ.items() if not is_secret_env(k, v)
    }


@dataclass
class Hook:
    event: str
    command: str
    matcher: re.Pattern | None = None
    blocking: bool = False
    pass_secrets: bool = False


_loaded = False
_hooks: dict[str, list[Hook]] = {}
_invalid_warned = False


def _parse_hooks(raw: Any) -> dict[str, list[Hook]]:
    out: dict[str, list[Hook]] = {ev: [] for ev in VALID_EVENTS}
    if not isinstance(raw, dict):
        return out
    for event, entries in raw.items():
        if event not in VALID_EVENTS:
            logger.warning("Unknown hook event '%s' (skipped)", event)
            continue
        if not isinstance(entries, list):
            logger.warning("Hook event '%s' must be a list (skipped)", event)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            command = entry.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            matcher_str = entry.get("matcher") or ""
            matcher = None
            if matcher_str:
                try:
                    matcher = re.compile(matcher_str)
                except re.error as e:
                    logger.warning(
                        "Invalid hook matcher '%s' (%s); ignoring matcher", matcher_str, e
                    )
            out[event].append(
                Hook(
                    event=event,
                    command=command,
                    matcher=matcher,
                    blocking=bool(entry.get("blocking", False)),
                    pass_secrets=bool(entry.get("pass_secrets", False)),
                )
            )
    return out


def _gate_project_hooks(
    parsed: dict[str, list[Hook]], path: Path | None
) -> dict[str, list[Hook]]:
    """#D301: hooks run shell (and `session_start` fires at BOOT). A
    `.alpha/settings.json` planted in an untrusted repo must not execute just
    because `alpha` was launched there. Fail-closed — hooks from project-scoped
    config are dropped unless ``ALPHA_TRUST_PROJECT_CONFIG`` is set. Config from
    `~/.alpha` (the user's own global) is always trusted.

    NOTE: this is the *minimal* mitigation. The full trust gate (an interactive
    "trust this folder?" prompt persisted per path+hash, plus refusing
    `pass_secrets` for project config as defense-in-depth) is tracked as the
    complete #D301/#D305 fix."""
    if is_user_scoped_config(path):
        return parsed  # ~/.alpha — the user's own global config, fully trusted
    if project_config_trusted():
        return parsed  # explicit opt-in via ALPHA_TRUST_PROJECT_CONFIG
    total = sum(len(v) for v in parsed.values())
    if total:
        logger.warning(
            "Ignoring %d project-scoped hook(s) from %s — untrusted (#D301, "
            "fail-closed). A .alpha/settings.json in an untrusted repo must not "
            "run shell at boot. Set ALPHA_TRUST_PROJECT_CONFIG=1 to opt in, or "
            "move hooks to ~/.alpha/settings.json.",
            total, path,
        )
    return {ev: [] for ev in VALID_EVENTS}


def load_hooks(force: bool = False) -> dict[str, list[Hook]]:
    """Read .alpha/settings.json and parse the hooks block. Cached after first call."""
    global _loaded, _hooks, _invalid_warned
    if _loaded and not force:
        return _hooks
    path = find_config_file("settings.json")
    raw = read_json_or_invalid(path, default={})
    if raw is JSON_INVALID:
        # #D807: settings.json exists but is unparseable — the user's hooks
        # (possibly a blocking pre_tool guardrail) can't be honored. Warn loudly
        # and do NOT cache the empty result, so a fixed file is picked up on the
        # next access instead of staying silently hookless for the session.
        if not _invalid_warned:
            _invalid_warned = True
            logger.warning(
                "settings.json at %s exists but could not be parsed — ALL hooks "
                "are inactive (including blocking pre_tool guardrails) until the "
                "JSON is fixed (#D807).",
                path,
            )
        return {ev: [] for ev in VALID_EVENTS}
    hooks_block = raw.get("hooks") if isinstance(raw, dict) else None
    _hooks = _gate_project_hooks(_parse_hooks(hooks_block), path)
    _loaded = True
    _invalid_warned = False
    return _hooks


def has_event(event: str) -> bool:
    """Cheap synchronous check: are any hooks registered for this event?"""
    return bool(load_hooks().get(event))


def _run_hook(hook: Hook, payload: dict, env_extra: dict[str, str]) -> tuple[int, str, str]:
    """Run a single hook command. Returns (exit_code, stdout, stderr).

    AUDIT_V2.3 #306: `subprocess.run(..., shell=True, timeout=...)` matava só
    o `/bin/sh` no timeout — netos (build/linter/testes lançados pelo hook)
    ficavam órfãos rodando. `start_new_session=True` dá um process group
    próprio ao hook e o timeout mata o GRUPO inteiro via killpg.
    """
    env = {**_hook_env(hook.pass_secrets), **env_extra}
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            hook.command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            # pgid próprio p/ matar o grupo no timeout (POSIX; no Windows
            # degrada para o comportamento antigo — só o shell é morto).
            start_new_session=(os.name == "posix"),
        )
        out, err = proc.communicate(
            input=json.dumps(payload, ensure_ascii=False, default=str),
            timeout=HOOK_TIMEOUT,
        )
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:  # Windows: sem process groups POSIX — mata só o filho
                    proc.kill()
            except OSError:
                pass
            # Recolhe o zumbi (pipes já fechados pelo kill do grupo).
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return 124, "", f"hook timed out after {HOOK_TIMEOUT}s"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


@dataclass
class HookOutcome:
    """Aggregated outcome of running all hooks for one event."""
    blocked: bool = False
    block_reason: str = ""
    stderr_lines: list[str] = field(default_factory=list)


_warned_substring_matchers: set[str] = set()


def _matches(hook: Hook, tool_name: str | None) -> bool:
    if hook.matcher is None:
        return True
    if tool_name is None:
        return False
    # #DL-004: anchor to the FULL tool name (fullmatch) instead of a substring
    # search. Unanchored `search("write")` matched `write_file` AND the
    # unrelated `rewrite` — a footgun. A matcher describes a tool NAME, so it
    # must consume the whole name: "write_file" and "write_file|edit_file"
    # (the documented forms) still match exactly; "write" matches nothing
    # (use "write.*" for a prefix or ".*write.*" for a substring on purpose).
    if hook.matcher.fullmatch(tool_name) is not None:
        return True
    # Migration aid: this is a silent behavior change. If the matcher WOULD have
    # fired under the old substring search, warn once per pattern so a user
    # upgrading notices their hook stopped firing instead of debugging silence.
    if hook.matcher.search(tool_name) is not None:
        pat = hook.matcher.pattern
        if pat not in _warned_substring_matchers:
            _warned_substring_matchers.add(pat)
            logger.warning(
                "Hook matcher %r matched tool %r as a substring but no longer "
                "fires — matchers now require a full tool-name match. Use "
                "'%s.*' for a prefix or '.*%s.*' for an intentional substring.",
                pat, tool_name, pat, pat,
            )
    return False


def fire(
    event: str,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    user_prompt: str | None = None,
    workspace: str | None = None,
    extra: dict | None = None,
) -> HookOutcome:
    """Fire all hooks registered for `event`. Synchronous (called via to_thread)."""
    hooks = load_hooks().get(event, [])
    if not hooks:
        return HookOutcome()

    payload: dict[str, Any] = {"event": event}
    env_extra: dict[str, str] = {"ALPHA_HOOK_EVENT": event}
    if tool_name is not None:
        payload["tool_name"] = tool_name
        env_extra["ALPHA_TOOL_NAME"] = tool_name
    if tool_args is not None:
        payload["tool_args"] = tool_args
        env_extra["ALPHA_TOOL_ARGS_JSON"] = json.dumps(
            tool_args, ensure_ascii=False, default=str
        )
    if user_prompt is not None:
        payload["user_prompt"] = user_prompt
        env_extra["ALPHA_USER_PROMPT"] = user_prompt
    if workspace:
        payload["workspace"] = workspace
        env_extra["ALPHA_WORKSPACE"] = workspace
    if extra:
        payload.update(extra)

    outcome = HookOutcome()
    for hook in hooks:
        if not _matches(hook, tool_name):
            continue
        exit_code, stdout, stderr = _run_hook(hook, payload, env_extra)
        if stderr.strip():
            outcome.stderr_lines.append(stderr.strip())
            logger.debug("hook[%s] stderr: %s", event, stderr.strip())
        if exit_code != 0 and hook.blocking:
            outcome.blocked = True
            reason = stderr.strip() or stdout.strip() or f"exit {exit_code}"
            outcome.block_reason = reason
            return outcome  # stop on first block
    return outcome


def reset_cache() -> None:
    """Force a re-read on next access. Useful for tests."""
    global _loaded, _hooks, _invalid_warned
    _loaded = False
    _hooks = {}
    _invalid_warned = False
