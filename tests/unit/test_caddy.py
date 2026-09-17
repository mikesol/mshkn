from __future__ import annotations

import json
import logging

import httpx
import pytest

from mshkn.errors import HostError
from mshkn.host.caddy import CaddyProxy


def make_proxy(handler: httpx.MockTransport) -> CaddyProxy:
    return CaddyProxy("http://caddy", "mshkn.dev", transport=handler)


async def test_add_route_inserts_the_regexp_route_at_the_head() -> None:
    """A computer's route goes in at index 0, never on the end.

    The terminal route (#189) has no matcher and so matches everything; it is
    only harmless while it is last. Caddy's admin API appends on POST to an
    array and inserts on PUT to an index, so a computer route must arrive by
    PUT at `routes/0` or the first one added would be shadowed forever.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.add_route("comp-1", "172.16.1.2")
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/config/apps/http/servers/main/routes/0"
    body = seen[0].content.decode()
    # httpx encodes json= with compact separators (no space after ":"), so
    # match that encoding rather than the brief's spaced literal.
    assert '"@id":"route-comp-1"' in body and "172.16.1.2:{http.regexp.port_match.1}" in body


async def test_ensure_terminal_route_appends_a_matcherless_404() -> None:
    """The catch-all is deleted and re-appended, so it is last however often it runs.

    Without it a `Host` matching no route falls off the end of the list and
    Caddy answers 200 with an empty body, which a tenant's `curl --fail` reads
    as success against a computer that no longer exists (#189).
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.ensure_terminal_route()
    assert [(r.method, r.url.path) for r in seen] == [
        ("DELETE", "/id/route-terminal"),
        ("POST", "/config/apps/http/servers/main/routes"),
    ]
    route = json.loads(seen[1].content.decode())
    assert route["@id"] == "route-terminal"
    assert "match" not in route, "a matcher would stop it catching the unrouted names"
    assert route["handle"] == [
        {"handler": "static_response", "status_code": 404, "body": "no such computer route\n"}
    ]


async def test_ensure_terminal_route_tolerates_a_missing_route_to_delete() -> None:
    """The first run on a host has nothing to delete; 404 is the normal case."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404 if request.method == "DELETE" else 200)

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.ensure_terminal_route()  # does not raise
    assert [r.method for r in seen] == ["DELETE", "POST"]


async def test_ensure_terminal_route_raises_host_error_when_the_install_fails() -> None:
    """Startup must not proceed silently: without the route the host lies to tenants."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.method == "DELETE" else 500, text="boom")

    proxy = make_proxy(httpx.MockTransport(handler))
    with pytest.raises(HostError):
        await proxy.ensure_terminal_route()


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
    """A 500 that is not the index race is reported once and not retried.

    This is the boundary of the retry added for #154: retrying a genuine
    failure would triple the admin API load for nothing.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="boom")

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        await proxy.remove_route("comp-1")  # does not raise
    assert len(seen) == 1, "only the index race is worth retrying"
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_remove_route_retries_when_concurrent_deletes_shift_the_index(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression test for #154.

    Caddy resolves an `@id` to an index in the routes array and deletes by
    index. Concurrent deletes shift those indexes under each other, so a
    delete can resolve an id to a slot that no longer holds it and answer 500
    `array index out of bounds`. The route is still there afterwards and
    nothing retried, so it leaked for good. Re-issuing the DELETE resolves the
    id afresh against the settled array, which is why a retry — not a
    different verb — is the fix: PATCH by `@id` resolves the index the same
    way.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(
                500,
                text='{"error":"[/config/apps/http/servers/main/routes/533] '
                'array index out of bounds: 533"}',
            )
        return httpx.Response(200)

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        await proxy.remove_route("comp-1")
    assert [(r.method, r.url.path) for r in seen] == [("DELETE", "/id/route-comp-1")] * 3
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a delete that succeeded on a retry is not a failure"
    )


async def test_remove_route_stops_retrying_the_index_error_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The retry is bounded: a route Caddy will not drop must not spin forever."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, text="array index out of bounds: 533")

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        await proxy.remove_route("comp-1")  # does not raise
    assert len(seen) == 3
    assert "Failed to remove Caddy route for comp-1" in caplog.text


async def test_remove_route_stops_retrying_once_the_route_is_gone() -> None:
    """A retry that finds a 404 is a success, not another attempt.

    The losing side of the race deleted the route; there is nothing left to do.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(500, text="array index out of bounds: 7")
        return httpx.Response(404, text="unknown object id")

    proxy = make_proxy(httpx.MockTransport(handler))
    await proxy.remove_route("comp-1")
    assert len(seen) == 2


async def test_list_route_ids_returns_the_installed_ids() -> None:
    """The sweep needs to know what Caddy is actually holding, not what we think."""
    routes = [
        {"@id": "route-comp-1", "handle": []},
        {"handle": []},  # a route nobody named
        {"@id": "route-api"},
        {"@id": "route-terminal"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == (
            "GET",
            "/config/apps/http/servers/main/routes",
        )
        return httpx.Response(200, json=routes)

    proxy = make_proxy(httpx.MockTransport(handler))
    assert await proxy.list_route_ids() == ["route-comp-1", "route-api", "route-terminal"]


async def test_list_route_ids_is_empty_when_the_admin_api_is_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It feeds a best-effort sweep, so it reports nothing rather than raising.

    An empty list makes the sweep a no-op. Raising would fail the whole reaper
    cycle over a maintenance task.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    proxy = make_proxy(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        assert await proxy.list_route_ids() == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_list_route_ids_is_empty_on_a_bad_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    proxy = make_proxy(httpx.MockTransport(lambda _: httpx.Response(500, text="boom")))
    with caplog.at_level(logging.WARNING):
        assert await proxy.list_route_ids() == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


async def test_list_route_ids_tolerates_an_empty_route_list() -> None:
    """Caddy answers `null`, not `[]`, for a server with no routes configured.

    The body is passed as raw bytes: `httpx.Response(json=None)` means "no JSON
    body at all" and would exercise the unparseable-body path instead.
    """
    proxy = make_proxy(httpx.MockTransport(lambda _: httpx.Response(200, content=b"null")))
    assert await proxy.list_route_ids() == []


async def test_list_route_ids_tolerates_a_body_that_is_not_json() -> None:
    proxy = make_proxy(httpx.MockTransport(lambda _: httpx.Response(200, text="<html>")))
    assert await proxy.list_route_ids() == []


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
