"""`/export [path] [--force]` — write the conversation to a Markdown file.

Renders `ctx.messages` (the live LLM history, not the display log): user
and assistant text, tool calls as one-liners and tool results as
collapsed blocks, clipped. The system prompt is left out. Default target
is `alpha-session-<timestamp>.md` in the current directory; an existing
file is never overwritten without `--force`.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from alpha._text_safety import strip_control_and_bidi
from alpha.display import C, c, print_error

from ._types import DispatchResult, ReplContext

_RESULT_CLIP = 1_500
_ARGS_CLIP = 300
_CWD_PREFIX = re.compile(r"^\[CWD: [^\]\n]*\]\n")


def _text(content) -> tuple[str, int]:
    """Plain text of a message content + number of image blocks."""
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        parts, images = [], 0
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif b.get("type") in ("image_url", "image"):
                images += 1
        return "\n".join(parts), images
    return "" if content is None else str(content), 0


def _clip(s: str, n: int) -> str:
    s = strip_control_and_bidi(s)
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} chars omitted]"


def _fence(s: str) -> str:
    # pick a fence longer than any backtick run inside the text
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    f = "`" * max(3, longest + 1)
    return f"{f}\n{s}\n{f}"


def render_markdown(ctx: ReplContext, *, now: float | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    model = ctx.cfg.get("model", "?") if isinstance(ctx.cfg, dict) else "?"
    out = [
        f"# Alpha session {ctx.session_id}",
        "",
        f"_Exported {stamp} · provider `{ctx.provider}` · model `{model}` · "
        f"{sum(1 for m in ctx.messages if m.get('role') != 'system')} messages_",
        "",
    ]
    tool_names: dict[str, str] = {}
    for m in ctx.messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            text, images = _text(m.get("content"))
            text = _CWD_PREFIX.sub("", text, count=1)
            note = f"\n\n_[{images} image(s) attached]_" if images else ""
            out += ["## User", "", strip_control_and_bidi(text).strip() + note, ""]
        elif role == "assistant":
            text, _ = _text(m.get("content"))
            if text.strip():
                out += ["## Assistant", "", strip_control_and_bidi(text).strip(), ""]
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                name = str(fn.get("name") or "?")
                args = fn.get("arguments") or ""
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                if tc.get("id"):
                    tool_names[str(tc["id"])] = name
                out += [f"**→ `{name}`** `{_clip(args, _ARGS_CLIP)}`", ""]
        elif role == "tool":
            name = m.get("name") or tool_names.get(str(m.get("tool_call_id")), "tool")
            text, _ = _text(m.get("content"))
            out += [
                "<details>", f"<summary>← <code>{name}</code> result</summary>", "",
                _fence(_clip(text, _RESULT_CLIP)), "", "</details>", "",
            ]
    return "\n".join(out).rstrip() + "\n"


def _handle_export(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    args = [a for a in parts[1:] if a != "--force"]
    force = "--force" in parts[1:]
    if len(args) > 1:
        print(f"  {c(C.YELLOW, 'Usage:')} /export [path.md] [--force]")
        return DispatchResult.CONTINUE
    if not any(m.get("role") != "system" for m in ctx.messages):
        print(f"  {c(C.GRAY, 'Nada para exportar ainda.')}")
        return DispatchResult.CONTINUE

    target = Path(args[0]).expanduser() if args else Path(
        f"alpha-session-{time.strftime('%Y%m%d-%H%M%S')}.md"
    )
    if target.is_dir():
        target = target / f"alpha-session-{time.strftime('%Y%m%d-%H%M%S')}.md"
    if target.exists() and not force:
        print_error(f"{target} já existe. Use /export {target} --force para sobrescrever.")
        return DispatchResult.CONTINUE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markdown(ctx), encoding="utf-8")
    except OSError as e:
        print_error(f"/export falhou: {e}")
        return DispatchResult.CONTINUE
    size = target.stat().st_size
    print(
        f"  {c(C.GREEN, '✦')} {c(C.CYAN, '/export')} "
        f"{c(C.GRAY, f'— {os.path.relpath(target) if not target.is_absolute() else target} ({size:,} bytes)')}"
    )
    return DispatchResult.CONTINUE
