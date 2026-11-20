"""`/remote` — follow and drive this session from a phone or browser.

Thin REPL front for `alpha/remote.py` (the server, token and event hub
live there). `start_remote` is also what `alpha --remote` calls.
"""

from __future__ import annotations

from alpha import remote
from alpha.display import C, c

from ._types import DispatchResult, ReplContext

_USAGE = (
    "/remote [on]        liga na rede local e mostra o link (com token) e o QR code\n"
    "  /remote on --local  liga só em 127.0.0.1 (para usar atrás do seu próprio túnel)\n"
    "  /remote off         desliga e invalida o link\n"
    "  /remote status      mostra o link de novo"
)


def _firewall_hint(host: str, port: int) -> str | None:
    """A ufw firewall with default-deny drops the phone's connection and the
    page just loads forever. We can't read the rules without root, so when
    ufw is enabled say how to open the port to the local network only."""
    try:
        with open("/etc/ufw/ufw.conf", encoding="utf-8") as fh:
            enabled = any(ln.strip().upper() == "ENABLED=YES" for ln in fh)
    except OSError:
        return None
    if not enabled:
        return None
    net = ".".join(host.split(".")[:3]) + ".0/24" if host.count(".") == 3 else "<sua-rede>/24"
    return f"sudo ufw allow from {net} to any port {port} proto tcp"


def _print_link(st: dict) -> None:
    print(f"  {c(C.VIOLET_GLOW + C.BOLD, '◆ Controle remoto ligado')}")
    print(f"    {c(C.CYAN, st['url'])}")
    for line in remote.qr_lines(st["url"]):
        print(f"    {line}")
    if st.get("loopback"):
        print(f"    {c(C.GRAY, 'Só nesta máquina (127.0.0.1). Aponte seu túnel para a porta ' + str(st['port']) + '.')}")
    else:
        print(f"    {c(C.GRAY, 'Abra no celular, na mesma rede Wi-Fi.')}")
        fw = _firewall_hint(str(st["host"]), int(st["port"]))
        if fw:
            print(
                f"    {c(C.YELLOW, '⚠')} "
                f"{c(C.GRAY, 'Firewall (ufw) ativo: se a página não abrir no celular, libere a porta:')}"
            )
            print(f"      {c(C.CYAN, fw)}")
    print(
        f"    {c(C.YELLOW, '⚠')} "
        f"{c(C.GRAY, 'Quem tiver este link conversa com o agente e aprova comandos neste computador.')}"
    )
    print(f"    {c(C.GRAY, 'HTTP sem criptografia: use só em rede confiável. /remote off desliga.')}")


def start_remote(model: str | None = None, local: bool = False, history: list | None = None) -> bool:
    """Start the server and print the link. Returns False (with a message)
    when it cannot run here."""
    from alpha.display.select import can_select
    from alpha.repl_input import use_simple_input

    if not remote.is_active():
        if not can_select() or use_simple_input():
            print(
                f"  {c(C.YELLOW, '⚠')} Controle remoto precisa do REPL interativo "
                f"{c(C.GRAY, '(terminal de verdade, sem ALPHA_SIMPLE_INPUT)')}."
            )
            return False
        try:
            remote.start(host="127.0.0.1" if local else None)
        except OSError as e:
            print(f"  {c(C.RED, 'Erro:')} {e}")
            return False
        import os

        remote.set_info(model=model, cwd=os.getcwd())
        if history:
            remote.seed_history(history)
    _print_link(remote.status())
    return True


def _handle_remote(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    args = parts[1:]
    action = args[0].lower() if args else "on"

    if action in ("off", "stop"):
        if remote.stop():
            print(f"  {c(C.GRAY, 'Controle remoto desligado — o link deixou de funcionar.')}")
        else:
            print(f"  {c(C.GRAY, 'Controle remoto já estava desligado.')}")
        return DispatchResult.CONTINUE

    if action == "status":
        st = remote.status()
        if not st["active"]:
            print(f"  {c(C.GRAY, 'Controle remoto desligado. /remote on liga.')}")
        else:
            _print_link(st)
            print(f"    {c(C.GRAY, str(st['clients']) + ' página(s) conectada(s)')}")
        return DispatchResult.CONTINUE

    if action in ("on", "start", "--local"):
        local = "--local" in args
        if remote.is_active() and local and not remote.status().get("loopback"):
            print(f"  {c(C.GRAY, 'Já está ligado na rede local. Use /remote off antes de trocar para --local.')}")
            return DispatchResult.CONTINUE
        start_remote(model=ctx.cfg.get("model"), local=local, history=ctx.history)
        return DispatchResult.CONTINUE

    print(f"  {c(C.YELLOW, 'Usage:')} {_USAGE}")
    return DispatchResult.CONTINUE
