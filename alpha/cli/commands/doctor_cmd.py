"""`/doctor` — environment health check, no network.

Each check returns ``(status, label, detail)`` with status ``ok``,
``warn`` or ``off`` (informational). Secrets are never printed — only
whether the env var is set. Every check is wrapped so a broken optional
subsystem shows as a warning instead of killing the command.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

from alpha.display import C, c

from ._types import DispatchResult, ReplContext

Check = tuple[str, str, str]  # status, label, detail

_ICON = {"ok": (C.GREEN, "✓"), "warn": (C.YELLOW, "!"), "off": (C.GRAY, "–")}


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("alpha-code")
    except Exception:
        return "?"


def _python() -> Check:
    v = sys.version_info
    s = f"{v.major}.{v.minor}.{v.micro}"
    return ("ok" if (v.major, v.minor) >= (3, 11) else "warn", "Python", s)


def _provider(ctx: ReplContext) -> Check:
    from alpha.config import _PROVIDERS, split_provider_spec
    name, _ = split_provider_spec(ctx.provider)
    cfg = _PROVIDERS.get(name)
    if not cfg:
        return ("warn", "Provider", f"{ctx.provider}: desconhecido")
    env = cfg.get("api_key_env")
    model = ctx.cfg.get("model", "?") if isinstance(ctx.cfg, dict) else "?"
    if not env:
        return ("ok", "Provider", f"{name} · {model} (sem chave)")
    if os.getenv(env):
        return ("ok", "Provider", f"{name} · {model} · {env} definida")
    return ("warn", "Provider", f"{name} · {env} NÃO definida")


def _other_providers(ctx: ReplContext) -> Check:
    from alpha.config import _PROVIDERS, split_provider_spec
    current, _ = split_provider_spec(ctx.provider)
    ready = sorted(
        n for n, cfg in _PROVIDERS.items()
        if n != current and (not cfg.get("api_key_env") or os.getenv(cfg["api_key_env"]))
    )
    return ("ok" if ready else "off", "Outros providers", ", ".join(ready) or "nenhum com chave")


def _bin(label: str, names: tuple[str, ...], *, required: bool = False, hint: str = "") -> Callable[[], Check]:
    def run() -> Check:
        found = [n for n in names if shutil.which(n)]
        if found:
            return ("ok", label, ", ".join(found))
        return ("warn" if required else "off", label, hint or f"não encontrado ({'/'.join(names)})")
    return run


def _playwright() -> Check:
    from alpha.config import browser_tools_enabled
    enabled = browser_tools_enabled()
    has = importlib.util.find_spec("playwright") is not None
    if not enabled:
        return ("off", "Browser tools", "desligadas (ALPHA_BROWSER_TOOLS)")
    if not has:
        return ("warn", "Browser tools", "playwright não instalado: pip install playwright && playwright install chromium")
    allow = os.getenv("ALPHA_BROWSER_ALLOWLIST", "")
    mode = "playwright ok"
    cdp = os.getenv("ALPHA_BROWSER_CDP", "").strip()
    if cdp:
        from alpha.tools.browser_session import cdp_endpoint
        try:
            mode = f"attach ao Chrome em {cdp_endpoint(cdp)}"
        except ValueError as e:
            return ("warn", "Browser tools", str(e))
    return ("ok" if allow else "warn", "Browser tools",
            mode + ("" if allow else " · ALPHA_BROWSER_ALLOWLIST vazia (qualquer domínio)"))


def _sandbox() -> Check:
    from alpha import shell_sandbox as sandbox
    d = sandbox.describe()
    status = "warn" if ("BROKEN" in d or "NO BACKEND" in d) else ("off" if "disabled" in d else "ok")
    return (status, "Sandbox", d.removeprefix("sandbox: "))


def _settings() -> Check:
    from alpha.settings import find_config_file
    p = find_config_file("settings.json")
    if not p:
        return ("off", "settings.json", "nenhum .alpha/settings.json")
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        return ("warn", "settings.json", f"{p}: inválido ({type(e).__name__})")
    perms = data.get("permissions") or {}
    hooks = data.get("hooks") or {}
    return ("ok", "settings.json", f"{p} · {len(perms.get('allow') or [])} allow / "
            f"{len(perms.get('deny') or [])} deny · {len(hooks)} hook event(s)")


def _mcp() -> Check:
    from alpha.mcp import loader
    from alpha.settings import find_config_file
    spec = find_config_file("mcp.json")
    active = loader.list_active_servers()
    if not spec and not active:
        return ("off", "MCP", "nenhum mcp.json")
    tools = sum(len(s["tools"]) for s in active)
    names = ", ".join(s["name"] for s in active) or "nenhum conectado"
    return ("ok" if active else "warn", "MCP", f"{len(active)} servidor(es): {names} · {tools} tool(s)")


def _project_context() -> Check:
    from alpha.project_context import CONTEXT_FILENAME, find_context_file
    p = find_context_file()
    return ("ok", CONTEXT_FILENAME, str(p)) if p else ("off", CONTEXT_FILENAME, "não encontrado (use /init)")


def _memory() -> Check:
    from alpha import memory
    if os.getenv("ALPHA_NO_MEMORY"):
        return ("off", "Memória", "desligada (ALPHA_NO_MEMORY)")
    ws, gl = memory.memory_path("workspace"), memory.memory_path("global")
    parts = [f"workspace {ws.stat().st_size:,}B" if ws.exists() else "workspace vazia",
             f"global {gl.stat().st_size:,}B" if gl.exists() else "global vazia"]
    return ("ok", "Memória", " · ".join(parts))


def _skills() -> Check:
    from alpha.skills import list_skills
    skills = list(list_skills())
    inactive = [s for s in skills if any(not shutil.which(b) for b in s.requires_bins)]
    detail = f"{len(skills)} registrada(s)"
    if inactive:
        detail += f" · {len(inactive)} sem bins: {', '.join(s.name for s in inactive[:5])}"
    return ("ok", "Skills", detail)


def _history_dir() -> Check:
    from alpha import history
    d = Path(getattr(history, "_HISTORY_DIR", Path.home() / ".alpha_code" / "history"))
    try:
        d.mkdir(parents=True, exist_ok=True)
        ok = os.access(d, os.W_OK)
    except OSError:
        ok = False
    return ("ok" if ok else "warn", "Histórico", f"{d}" + ("" if ok else " (sem escrita)"))


def _notify() -> Check:
    from alpha import notify
    from alpha.config import notify_mode
    mode = notify_mode()
    if mode == "off":
        return ("off", "Notificações", "off")
    return ("ok", "Notificações", f"{mode} · backend {notify._backend()}")


def run_checks(ctx: ReplContext) -> list[Check]:
    checks: list[Callable[[], Check]] = [
        _python,
        lambda: ("ok", "alpha-code", _version()),
        lambda: _provider(ctx),
        lambda: _other_providers(ctx),
        _bin("git", ("git",), required=True),
        _bin("gh", ("gh",), hint="não encontrado — /review <PR#> e --comment precisam do GitHub CLI"),
        _playwright,
        _sandbox,
        _settings,
        _mcp,
        _project_context,
        _memory,
        _skills,
        _history_dir,
        _notify,
    ]
    out: list[Check] = []
    for fn in checks:
        try:
            out.append(fn())
        except Exception as e:
            out.append(("warn", getattr(fn, "__name__", "check").lstrip("_"), f"falhou: {type(e).__name__}: {e}"))
    return out


def _handle_doctor(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    results = run_checks(ctx)
    width = max(len(r[1]) for r in results)
    for status, label, detail in results:
        color, icon = _ICON.get(status, _ICON["warn"])
        print(f"  {c(color, icon)} {label.ljust(width)}  {c(C.GRAY, detail)}")
    warns = sum(1 for r in results if r[0] == "warn")
    tail = c(C.YELLOW, f"{warns} aviso(s)") if warns else c(C.GREEN, "tudo ok")
    print(f"  {c(C.GRAY_DARK, f'{len(results)} verificações ·')} {tail}")
    return DispatchResult.CONTINUE
