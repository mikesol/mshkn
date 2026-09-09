"""The relay's SSRF guard (#110), ported from lampas: a target may be http or
https, may not be a raw private or reserved address, and every address its
hostname resolves to is checked. Two deviations from lampas, on purpose: a
hostname that does not resolve is refused (lampas failed open behind
Cloudflare's own fetch guard; mshkn has none), and there is no allowlist or off
switch (the flow tier injects a resolver instead)."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    Resolver = Callable[[str], Awaitable[list[str]]]

Address = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
# Lampas's list. 172.16.0.0/12 is also the VM and Docker range on the host.
BLOCKED_RANGES: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)
_NETWORKS = tuple(ipaddress.ip_network(r) for r in BLOCKED_RANGES)


def parse_address(text: str) -> Address | None:
    """The address `text` names, with an IPv4-mapped IPv6 address unwrapped; None for a name."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def blocked_reason(address: Address) -> str | None:
    for network in _NETWORKS:
        if address.version == network.version and address in network:
            return f"{address} is in the blocked range {network}"
    return None


def check_url(url: str) -> tuple[str, str | None]:
    """(hostname, reason): the scheme and a raw address are checked here; a name
    is returned for the caller to resolve. `reason` is None when nothing is wrong."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        return "", "invalid URL"
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return "", f"blocked scheme {parts.scheme or '(none)'!r}: only http and https"
    if not hostname:
        return "", "invalid URL: no host"
    address = parse_address(hostname)
    if address is not None:
        reason = blocked_reason(address)
        if reason is not None:
            return hostname, f"blocked address: {reason}"
    return hostname, None


async def resolve_host(hostname: str) -> list[str]:
    """Every address the process's resolver returns for the name, A and AAAA."""
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


async def guard(url: str, resolver: Resolver) -> str | None:
    """The reason `url` may not be called, or None. Resolves a hostname through `resolver`."""
    hostname, reason = check_url(url)
    if reason is not None:
        return reason
    if parse_address(hostname) is not None:
        return None
    try:
        addresses = await resolver(hostname)
    except OSError as exc:
        return f"{hostname} does not resolve: {exc}"
    if not addresses:
        return f"{hostname} does not resolve to any address"
    for text in addresses:
        address = parse_address(text)
        if address is None:
            return f"{hostname} resolves to an unparseable address {text!r}"
        blocked = blocked_reason(address)
        if blocked is not None:
            return f"{hostname} resolves to a blocked address: {blocked}"
    return None
