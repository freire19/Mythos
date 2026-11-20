"""Shared SSRF-protection utilities.

Centralises ``is_private_ip``, ``is_private_ip_address``,
``validate_url`` and ``resolve_and_validate`` so every HTTP path in the
codebase uses the **same** fail-closed semantics.

Fail-closed means: if DNS resolution fails or the IP cannot be parsed,
the address is treated as private/blocked.  This is the secure default.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Teto de espera por resolução DNS no path async (#D802) — um resolver
# black-holed não pode segurar o caller pelo budget do SO (~20-30s).
_DNS_TIMEOUT = 5.0


# #D315: redes que o módulo ipaddress NÃO marca como privadas/reservadas mas
# que alcançam infra interna/relay e que `is_global` classifica errado — CGNAT
# (RFC 6598, usado pelo Tailscale), 6to4 anycast, Teredo, benchmarking.
_EXTRA_DENY_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "100.64.0.0/10",   # CGNAT / Shared Address Space (Tailscale)
        "192.88.99.0/24",  # 6to4 anycast relay (is_global=True)
        "198.18.0.0/15",   # benchmarking (redundante com is_private, defensivo)
        "2002::/16",       # 6to4
        "2001::/32",       # Teredo
    )
)


def is_private_ip_address(ip_str: str) -> bool:
    """Return *True* if *ip_str* is a private / reserved / loopback address.

    **Fail-closed**: returns ``True`` on any parse error.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or not addr.is_global  # #D315: pega CGNAT e demais non-global
        ):
            return True
        # #D315: anycast/relay que o ipaddress considera global.
        return any(addr in net for net in _EXTRA_DENY_NETS)
    except ValueError:
        return True


def is_private_ip(hostname: str) -> bool:
    """Return *True* if *hostname* resolves to any private / reserved IP.

    **Fail-closed**: returns ``True`` when DNS resolution fails.
    """
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        for _family, _, _, _, sockaddr in addr_info:
            if is_private_ip_address(sockaddr[0]):
                return True
        return False
    except (socket.gaierror, ValueError, OSError):
        return True  # fail-closed


def validate_url_syntax(url: str) -> str | None:
    """Syntactic-only URL validation (scheme + hostname) — **no DNS**.

    Async callers should pair this with ``resolve_and_validate`` so the
    IP check happens once, without blocking the event-loop thread.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"

    if parsed.scheme not in ("http", "https"):
        return f"Scheme '{parsed.scheme}' not allowed. Use http or https."

    if not parsed.hostname:
        return "URL without hostname"

    return None


def validate_url(url: str) -> str | None:
    """Validate *url* for SSRF safety.

    Returns an error message (``str``) when the URL must be rejected,
    or ``None`` when the URL is safe to fetch.
    """
    err = validate_url_syntax(url)
    if err:
        return err

    hostname = urlparse(url).hostname
    if is_private_ip(hostname):
        return f"SSRF blocked: {hostname} resolves to private/reserved IP"

    return None


async def resolve_and_validate(hostname: str) -> str:
    """Resolve *hostname* once and validate the IP against SSRF rules.

    Returns the resolved IP string.  Raises ``ValueError`` when DNS
    fails or the IP is private (prevents DNS-rebinding attacks).
    """
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(_DNS_TIMEOUT):
            infos = await loop.getaddrinfo(
                hostname, None, family=socket.AF_UNSPEC
            )
    except (socket.gaierror, OSError, asyncio.TimeoutError):
        raise ValueError(f"DNS resolution failed for {hostname}")

    if not infos:
        raise ValueError(f"DNS resolution failed for {hostname}")
    # Check ALL resolved addresses, not just the first — an attacker
    # can mix public+private records and AF_UNSPEC returns both families.
    for _, _, _, _, sockaddr in infos:
        if is_private_ip_address(sockaddr[0]):
            raise ValueError(
                f"IP {sockaddr[0]} is private (resolved from {hostname})"
            )
    return infos[0][4][0]
