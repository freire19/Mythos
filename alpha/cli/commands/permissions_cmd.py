"""`/permissions` — list and edit the allow/deny rules of `.alpha/settings.json`.

    /permissions                       show rules (and where they come from)
    /permissions allow <rule>          add an allow rule
    /permissions deny <rule>           add a deny rule
    /permissions remove <rule|#n>      drop a rule by text or by its number

Rule grammar is `approval._parse_rule`'s: `tool`, `tool(literal)` or
`tool:regex` (e.g. `execute_shell:^npm `). Rules are validated before
being written; the file written is the one `find_config_file` actually
loads (or `~/.alpha/settings.json` when none exists), and the rule cache
is invalidated so the change applies to the next tool call.
"""

from __future__ import annotations

import json
from pathlib import Path

from alpha import approval
from alpha.display import C, c, print_error
from alpha.settings import read_json

from ._types import DispatchResult, ReplContext

_USAGE = (
    "/permissions                  lista as regras allow/deny\n"
    "  /permissions allow <regra>    adiciona allow (ex: execute_shell:^npm )\n"
    "  /permissions deny <regra>     adiciona deny  (ex: execute_shell(rm -rf /))\n"
    "  /permissions remove <regra|#n> remove pela regra ou pelo número da lista\n"
    "  regra: tool | tool(literal) | tool:regex"
)


def _rules_from_disk(path: Path | None) -> tuple[list[str], list[str]]:
    data = read_json(path, default={}) if path else {}
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return [], []
    allow = [str(r) for r in (perms.get("allow") or []) if isinstance(r, str)]
    deny = [str(r) for r in (perms.get("deny") or []) if isinstance(r, str)]
    return allow, deny


def _write(path: Path, allow: list[str], deny: list[str]) -> None:
    data = read_json(path, default={})
    if not isinstance(data, dict):
        data = {}
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    perms["allow"], perms["deny"] = allow, deny
    data["permissions"] = perms
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    approval.reset_permission_cache()


def _numbered(allow: list[str], deny: list[str]) -> list[tuple[int, str, str]]:
    rows = [(i, "allow", r) for i, r in enumerate(allow, 1)]
    rows += [(i, "deny", r) for i, r in enumerate(deny, len(allow) + 1)]
    return rows


def _print_list(path: Path | None, allow: list[str], deny: list[str]) -> None:
    src = str(path) if path else "nenhum settings.json (regras built-in apenas)"
    print(f"  {c(C.CYAN + C.BOLD, 'Permissões')} {c(C.GRAY, '· ' + src)}")
    if not allow and not deny:
        print(f"  {c(C.GRAY, 'Sem regras do usuário. Built-in: tools SAFE auto-aprovadas, shell allowlistado; o resto pergunta.')}")
        return
    for n, kind, rule in _numbered(allow, deny):
        color = C.GREEN if kind == "allow" else C.RED
        bad = approval._parse_rule(rule) is None
        flag = c(C.YELLOW, "  (inválida, ignorada)") if bad else ""
        print(f"  {c(C.GRAY_DARK, f'#{n:<2}')} {c(color, kind.ljust(5))} {rule}{flag}")
    print(f"  {c(C.GRAY_DARK, 'deny vence allow; deny é bloqueio sem prompt. /sandbox e /accept-edits mostram o resto do estado.')}")


def _handle_permissions(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    args = parts[1:]
    path = approval._find_settings_file()
    allow, deny = _rules_from_disk(path)

    if not args or args[0] in ("list", "ls"):
        _print_list(path, allow, deny)
        return DispatchResult.CONTINUE

    action = args[0].lower()
    rule = " ".join(args[1:]).strip()
    if action not in ("allow", "deny", "remove", "rm") or not rule:
        print(f"  {c(C.YELLOW, 'Usage:')} {_USAGE}")
        return DispatchResult.CONTINUE

    write_path = path or approval._settings_write_path()

    if action in ("remove", "rm"):
        target = rule
        if rule.startswith("#") and rule[1:].isdigit():
            rows = {n: (kind, r) for n, kind, r in _numbered(allow, deny)}
            hit = rows.get(int(rule[1:]))
            if not hit:
                print_error(f"Não existe regra {rule}.")
                return DispatchResult.CONTINUE
            target = hit[1]
        before = len(allow) + len(deny)
        allow = [r for r in allow if r != target]
        deny = [r for r in deny if r != target]
        if len(allow) + len(deny) == before:
            print_error(f"Regra não encontrada: {target}")
            return DispatchResult.CONTINUE
        _write(write_path, allow, deny)
        print(f"  {c(C.GREEN, '✦')} removida: {c(C.GRAY, target)}")
        return DispatchResult.CONTINUE

    if approval._parse_rule(rule) is None:
        print_error(f"Regra inválida: {rule}  (formato: tool | tool(literal) | tool:regex)")
        return DispatchResult.CONTINUE
    bucket = allow if action == "allow" else deny
    if rule in bucket:
        print(f"  {c(C.GRAY, f'{action} já contém: {rule}')}")
        return DispatchResult.CONTINUE
    bucket.append(rule)
    _write(write_path, allow, deny)
    color = C.GREEN if action == "allow" else C.RED
    print(f"  {c(C.GREEN, '✦')} {c(color, action)} {rule} {c(C.GRAY, f'→ {write_path}')}")
    return DispatchResult.CONTINUE
