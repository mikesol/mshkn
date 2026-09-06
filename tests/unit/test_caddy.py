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


async def test_add_route_wraps_other_httpx_errors_as_host_error() -> None:
    """A ReadTimeout is not retried; it must still leave as a HostError, not raw httpx."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("admin API stalled")

    proxy = make_proxy(httpx.MockTransport(handler))
    with pytest.raises(HostError):
        await proxy.add_route("comp-1", "172.16.1.2")


async def test_remove_route_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """A transport error is swallowed, but the delete is still issued and logged.

    Callers do not guard `remove_route`, so it must not raise. Asserting only
    that would hold just as well for a body that never sent the request, so the
    single DELETE aimed at this computer's route id is what is pinned.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise httpx.RemoteProtocolError("Server disconnected")

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        await proxy.remove_route("comp-1")
    assert [(r.method, r.url.path) for r in seen] == [("DELETE", "/id/route-comp-1")], (
        "one delete, aimed at this computer's route, and no retry"
    )
    assert "Failed to remove Caddy route for comp-1" in caplog.text


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


async def test_close_makes_the_proxy_unusable() -> None:
    """close() releases the admin client; healthy() reports it, rather than raising."""
    proxy = make_proxy(httpx.MockTransport(lambda _: httpx.Response(200)))
    assert await proxy.healthy()
    await proxy.close()
    assert await proxy.healthy() is False
