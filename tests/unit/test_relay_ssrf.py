"""The relay's SSRF guard, ported from lampas (#110): http and https only, no raw
private or reserved address, every resolved address checked, an unresolvable
host refused (lampas failed open; mshkn has no second guard behind it)."""

from __future__ import annotations

import pytest

from mshkn.services.ssrf import blocked_reason, check_url, guard, parse_address


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "10.1.2.3",
        "127.0.0.1",
        "127.255.255.254",
        "169.254.169.254",
        "172.16.254.1",
        "172.31.0.9",
        "192.168.1.1",
        "::1",
        "fc00::1",
        "fd12::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_private_and_reserved_addresses_are_blocked(address: str) -> None:
    parsed = parse_address(address)
    assert parsed is not None
    assert blocked_reason(parsed) is not None


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "65.21.22.161", "172.32.0.1", "2606:2800:220:1:248:1893:25c8:1946"]
)
def test_public_addresses_are_allowed(address: str) -> None:
    parsed = parse_address(address)
    assert parsed is not None
    assert blocked_reason(parsed) is None


def test_parse_address_returns_none_for_a_hostname() -> None:
    assert parse_address("api.anthropic.com") is None
    assert parse_address("") is None


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("ftp://example.com/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("example.com/no-scheme", "scheme"),
        ("http:///path", "host"),
        ("http://[::1", "invalid"),
        ("http://127.0.0.1:8000/health", "blocked"),
        ("http://[::1]/", "blocked"),
        ("http://[::ffff:10.0.0.1]/", "blocked"),
        ("http://172.16.254.1/", "blocked"),
    ],
)
def test_check_url_refuses_bad_schemes_and_raw_blocked_addresses(url: str, fragment: str) -> None:
    _, reason = check_url(url)
    assert reason is not None and fragment in reason


def test_check_url_passes_a_public_address_and_returns_a_hostname_to_resolve() -> None:
    assert check_url("https://93.184.216.34/v1") == ("93.184.216.34", None)
    assert check_url("HTTPS://api.anthropic.com:443/v1/messages") == ("api.anthropic.com", None)
    assert check_url("https://[2606:2800:220:1:248:1893:25c8:1946]/") == (
        "2606:2800:220:1:248:1893:25c8:1946",
        None,
    )


async def test_guard_resolves_hostnames_and_refuses_a_private_answer() -> None:
    table = {
        "api.anthropic.com": ["160.79.104.10"],
        "dual.example": ["93.184.216.34", "10.0.0.5"],
        "local.example": ["127.0.0.1"],
        "six.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    }

    async def resolver(hostname: str) -> list[str]:
        if hostname not in table:
            raise OSError("no such host")
        return table[hostname]

    assert await guard("https://api.anthropic.com/v1/messages", resolver) is None
    assert await guard("https://six.example/", resolver) is None
    assert await guard("https://93.184.216.34/", resolver) is None  # raw: never resolved
    dual = await guard("https://dual.example/", resolver)
    assert dual is not None and "dual.example" in dual and "10.0.0.5" in dual
    local = await guard("http://local.example/", resolver)
    assert local is not None and "127.0.0.1" in local
    missing = await guard("https://nowhere.example/", resolver)
    assert missing is not None and "resolve" in missing
    scheme = await guard("ftp://api.anthropic.com/", resolver)
    assert scheme is not None and "scheme" in scheme


async def test_guard_refuses_a_host_that_resolves_to_nothing() -> None:
    async def resolver(hostname: str) -> list[str]:
        return []

    reason = await guard("https://empty.example/", resolver)
    assert reason is not None and "resolve" in reason


async def test_guard_refuses_a_host_that_resolves_to_an_unparseable_address() -> None:
    async def resolver(hostname: str) -> list[str]:
        return ["not-an-ip"]

    reason = await guard("https://unparseable.example/", resolver)
    assert reason is not None and "unparseable.example" in reason and "unparseable" in reason
