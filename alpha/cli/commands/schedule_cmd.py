"""`/loop` — schedule a prompt or slash command on an interval (REPL only)."""

from __future__ import annotations

from alpha import scheduler
from alpha.display import C, c

from ._types import DispatchResult, ReplContext

_USAGE = (
    "/loop <intervalo> <prompt>      repete o prompt a cada intervalo (ex: /loop 5m /tasks)\n"
    "  /loop once <intervalo> <prompt> dispara uma única vez (ex: /loop once 20m checa o deploy)\n"
    "  /loop list                      lista os loops ativos\n"
    "  /loop stop <id|all>             cancela\n"
    "  intervalo: 30s, 5m, 1h (mín 10s, máx 24h). Dispara só com o REPL ocioso."
)


def _print_list() -> None:
    items = scheduler.list_schedules()
    if not items:
        print(f"  {c(C.GRAY, 'Nenhum loop ativo.')}")
        return
    print(f"  {c(C.CYAN + C.BOLD, 'Loops ativos')}")
    for s in items:
        kind = c(C.GRAY, "once") if s["max_fires"] == 1 else c(C.GRAY, f"every {s['interval']}")
        nxt = c(C.GRAY_DARK, f"próximo em {int(s['next_in_s'])}s · {s['fires']}x")
        print(f"  #{s['id']} {kind} {nxt}  {s['prompt'][:80]}")


def _handle_loop(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    args = parts[1:]
    if not args or args[0] == "list":
        _print_list()
        return DispatchResult.CONTINUE

    if args[0] == "stop":
        target = args[1] if len(args) > 1 else ""
        if target == "all":
            n = scheduler.clear()
            print(f"  {c(C.GRAY, f'{n} loop(s) cancelado(s).')}")
        elif target.isdigit() and scheduler.remove(int(target)) is not None:
            print(f"  {c(C.GRAY, f'Loop #{target} cancelado.')}")
        else:
            print(f"  {c(C.YELLOW, 'Usage:')} /loop stop <id|all>")
        return DispatchResult.CONTINUE

    max_fires = None
    if args[0] == "once":
        max_fires = 1
        args = args[1:]
    if len(args) < 2:
        print(f"  {c(C.YELLOW, 'Usage:')} {_USAGE}")
        return DispatchResult.CONTINUE
    try:
        interval = scheduler.parse_interval(args[0])
        s = scheduler.add(interval, " ".join(args[1:]), max_fires=max_fires)
    except ValueError as e:
        print(f"  {c(C.RED, 'Erro:')} {e}")
        return DispatchResult.CONTINUE
    when = "uma vez" if max_fires == 1 else "a cada"
    print(
        f"  {c(C.GREEN, '⏰')} Loop #{s.id} agendado — {when} "
        f"{scheduler.format_interval(interval)}: {c(C.GRAY, s.prompt[:80])}"
    )
    print(f"  {c(C.GRAY_DARK, 'Dispara quando o REPL estiver ocioso. /loop stop ' + str(s.id) + ' cancela.')}")
    return DispatchResult.CONTINUE
