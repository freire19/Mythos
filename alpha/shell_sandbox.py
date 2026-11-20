"""Optional process sandbox for destructive shell tools (Plano-Upgrade-v3 H3 #14).

Off by default. When enabled in `.alpha/settings.json` under the `sandbox`
key, shell-class tools (`execute_shell`, `execute_pipeline`, …) wrap their
subprocess invocations through `firejail` or `bubblewrap` so a compromised
or careless command can't reach the rest of the filesystem or the network.

Why opt-in and not on-by-default:
  - Linux-only — macOS and Windows would silently no-op, giving a false sense
    of safety. Surfacing this as an explicit setting avoids that footgun.
  - Sandboxing imposes real constraints (no network, locked-down filesystem)
    that break legitimate workflows (pip install, curl, git push). Users opt
    in only when their threat model justifies the friction.

Settings shape (`.alpha/settings.json`):

    {
      "sandbox": {
        "enabled": true,
        "tool": "auto",          # "auto" | "firejail" | "bubblewrap"
        "deny_network": true,    # --net=none / --unshare-net
        "extra_args": []         # appended to the sandbox prefix
      }
    }

Environment override: `ALPHA_SANDBOX=1` forces enabled=true with defaults,
useful for CI smoke tests without editing settings.

Failure mode: if `enabled=true` but no sandbox binary is on PATH, every
sandboxed call raises `SandboxUnavailableError`. Fail-closed is the only
defensible choice — silently dropping the sandbox would defeat the request.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Literal

from .settings import JSON_INVALID, find_config_file, read_json_or_invalid

SandboxTool = Literal["auto", "firejail", "bubblewrap"]


class SandboxUnavailableError(RuntimeError):
    """Sandbox was requested but no usable backend was found.

    Surfaced through the tool result so the agent sees a clear error
    instead of silently bypassing the requested sandbox."""


@dataclass
class SandboxConfig:
    enabled: bool = False
    tool: SandboxTool = "auto"
    deny_network: bool = True
    extra_args: list[str] = field(default_factory=list)
    # #D807: settings.json exists but could not be parsed — the sandbox state
    # is unknowable, so treat it as enabled-but-broken (fail-closed).
    config_error: str = ""


def load_config() -> SandboxConfig:
    """Build a SandboxConfig from settings.json + ALPHA_SANDBOX env var.

    The env var is a coarse override — set it for quick experiments,
    use settings.json for anything persistent. Env wins over file when
    set to a truthy value; otherwise file wins.
    """
    path = find_config_file("settings.json")
    data = read_json_or_invalid(path, default={})
    if data is JSON_INVALID:
        return SandboxConfig(
            enabled=True,
            config_error=(
                f"settings.json at {path} exists but could not be parsed, so the "
                "sandbox opt-in state is unknowable — refusing to run unsandboxed "
                "(fail-closed); fix the JSON syntax or remove the file"
            ),
        )
    data = data or {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("sandbox")
    if not isinstance(raw, dict):
        raw = {}

    env_force = os.environ.get("ALPHA_SANDBOX", "").lower() in ("1", "true", "yes")
    enabled = bool(raw.get("enabled", False)) or env_force
    tool = raw.get("tool", "auto")
    if tool not in ("auto", "firejail", "bubblewrap"):
        tool = "auto"

    extra_args = raw.get("extra_args")
    if not isinstance(extra_args, list):
        extra_args = []

    return SandboxConfig(
        enabled=enabled,
        tool=tool,  # type: ignore[arg-type]
        deny_network=bool(raw.get("deny_network", True)),
        extra_args=list(extra_args),
    )


def resolve_tool(preferred: SandboxTool) -> tuple[str, str] | None:
    """Return (tool_name, absolute_path) for the requested sandbox backend.

    SECURITY_TIER0: `auto` now prefers bubblewrap over firejail. bwrap gives
    real filesystem containment (`--ro-bind / /` + a writable workspace bind),
    while firejail — even with the home-containment flags below — starts from a
    fully-visible host filesystem and can only restrict it best-effort. When
    both are installed and the user hasn't pinned one, the stronger backend
    wins. Returns None when no backend is installed.
    """
    if preferred == "firejail":
        path = shutil.which("firejail")
        return ("firejail", path) if path else None
    if preferred == "bubblewrap":
        path = shutil.which("bwrap")
        return ("bubblewrap", path) if path else None
    # auto — prefer bubblewrap (stronger containment), fall back to firejail.
    bw = shutil.which("bwrap")
    if bw:
        return ("bubblewrap", bw)
    fj = shutil.which("firejail")
    if fj:
        return ("firejail", fj)
    return None


def _firejail_prefix(binary: str, *, cwd: str | None, cfg: SandboxConfig) -> list[str]:
    """Build the firejail argv prefix.

    `--quiet` suppresses firejail's own banner so the tool output stays clean.
    `--private-tmp` gives the sandboxed process its own /tmp without exposing
    the host's; legitimate /tmp usage still works (compilers, pip).

    SECURITY_TIER0 — filesystem containment. The previous prefix set only
    `--net=none --private-tmp`, leaving the sandboxed process with read/write
    access to the entire `$HOME` (it could still read `~/.ssh/id_rsa` or plant
    a payload in `~/.bashrc`), directly contradicting this module's docstring.
    We now mount the home directory read-only and carve the workspace (`cwd`)
    back to read-write, so a command can edit its project but can't touch the
    rest of the user's files. Firejail applies `--read-write` after
    `--read-only`, so the later workspace carve-out wins.

    This is best-effort: firejail starts from a fully-visible host filesystem,
    so a workspace outside `$HOME` (or reads under `/etc`) are not covered as
    tightly as bubblewrap's `--ro-bind / /`. `resolve_tool` prefers bubblewrap
    in `auto` for exactly this reason; firejail is the fallback.
    """
    args = [binary, "--quiet"]
    if cfg.deny_network:
        args.append("--net=none")
    args.append("--private-tmp")
    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        args.append(f"--read-only={home}")
    if cwd:
        # Workspace stays writable; carved out of the read-only home above.
        args.append(f"--read-write={os.path.abspath(cwd)}")
    args.extend(cfg.extra_args)
    args.append("--")
    return args


def _bwrap_prefix(binary: str, *, cwd: str | None, cfg: SandboxConfig) -> list[str]:
    """Build the bubblewrap argv prefix.

    Bubblewrap needs an explicit filesystem layout. This config:
      - Read-only binds the entire root so binaries and libraries work
      - Writable /tmp via tmpfs (matches firejail's `--private-tmp`)
      - When `cwd` is provided, bind it read-write so the agent can edit
        its workspace; without this, `--ro-bind /` would block writes.
      - `--die-with-parent` ensures the sandbox can't outlive the agent.
      - `--unshare-net` cuts off network when deny_network is set.
    """
    args = [
        binary,
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", "/", "/",
    ]
    if cwd:
        args.extend(["--bind", cwd, cwd])
        args.extend(["--chdir", cwd])
    if cfg.deny_network:
        args.append("--unshare-net")
    args.extend(cfg.extra_args)
    args.append("--")
    return args


def wrap_command(
    cmd_parts: list[str],
    *,
    cwd: str | None = None,
    cfg: SandboxConfig | None = None,
) -> list[str]:
    """Return `cmd_parts` (possibly) prefixed with a sandbox invocation.

    When sandbox is disabled, this is a passthrough — callers can call it
    unconditionally without an extra branch. When enabled but no backend
    is installed, raises `SandboxUnavailableError`.
    """
    cfg = cfg or load_config()
    if cfg.config_error:
        raise SandboxUnavailableError(cfg.config_error)
    if not cfg.enabled:
        return list(cmd_parts)

    resolved = resolve_tool(cfg.tool)
    if resolved is None:
        raise SandboxUnavailableError(
            f"sandbox enabled but no backend available (preferred={cfg.tool!r}); "
            "install firejail (apt install firejail) or bubblewrap (apt install bubblewrap), "
            "or set sandbox.enabled=false in .alpha/settings.json"
        )

    tool_name, tool_path = resolved
    if tool_name == "firejail":
        prefix = _firejail_prefix(tool_path, cwd=cwd, cfg=cfg)
    else:
        prefix = _bwrap_prefix(tool_path, cwd=cwd, cfg=cfg)
    return prefix + list(cmd_parts)


def describe() -> str:
    """One-line human description of the active sandbox state.

    Used by the `/sandbox` REPL command and logged at startup when the
    user has sandbox enabled, so the constraints are visible up front.
    """
    cfg = load_config()
    if cfg.config_error:
        return f"sandbox: BROKEN CONFIG — {cfg.config_error}"
    if not cfg.enabled:
        return "sandbox: disabled"
    resolved = resolve_tool(cfg.tool)
    if resolved is None:
        return f"sandbox: enabled but NO BACKEND ({cfg.tool}) — commands will fail"
    name, _ = resolved
    net = "no network" if cfg.deny_network else "network allowed"
    return f"sandbox: {name} ({net})"
