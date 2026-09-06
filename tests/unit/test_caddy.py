from __future__ import annotations

import logging

import httpx
import pytest

from mshkn.errors import HostError
from mshkn.host.caddy import CaddyProxy


def make_proxy(handler: httpx.MockTransport) -> CaddyProxy:
    return CaddyProxy("http://caddy", "mshkn.dev", transport=handler)


async def test_add_route_posts_regexp_route() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.add_route("comp-1", "172.16.1.2")
    assert seen[0].method == "POST" and seen[0].url.path == "/config/apps/http/servers/main/routes"
    body = seen[0].content.decode()
    # httpx encodes json= with compact separators (no space after ":"), so
    # match that encoding rather than the brief's spaced literal.
    assert '"@id":"route-comp-1"' in body and "172.16.1.2:{http.regexp.port_match.1}" in body


async def test_add_route_raises_host_error_after_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    proxy = make_proxy(httpx.MockTransport(handler))
    with pytest.raises(HostError):
        await proxy.add_route("comp-1", "172.16.1.2")


async def test_add_route_raises_host_error_on_bad_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    proxy = make_proxy(httpx.MockTransport(handler))
    with pytest.raises(HostError):
        await proxy.add_route("comp-1", "172.16.1.2")


async def test_remove_route_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected")

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.remove_route("comp-1")  # logs, does not raise


async def test_remove_route_treats_404_as_success(caplog: pytest.LogCaptureFixture) -> None:
    """Regression test: concurrent deletes of the same computer previously
    surfaced route-already-gone as a warning-level failure. A 404 (route
    absent) is the expected outcome of a race and must not be logged as one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="unknown object id")

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        await proxy.remove_route("comp-1")  # does not raise
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_remove_route_logs_warning_on_other_bad_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        await proxy.remove_route("comp-1")  # does not raise
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_healthy_reflects_admin_api() -> None:
    proxy = make_proxy(httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    assert await proxy.healthy()

    def raise_connect_error(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    down = make_proxy(httpx.MockTransport(raise_connect_error))
    assert not await down.healthy()


async def test_close_closes_client() -> None:
    proxy = make_proxy(httpx.MockTransport(lambda _: httpx.Response(200)))
    await proxy.close()
    assert proxy._client.is_closed
