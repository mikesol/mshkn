from __future__ import annotations

import httpx

from mshkn.services.callback import deliver_callback


async def test_delivers_once_on_success() -> None:
    seen: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers) | {"body": request.content.decode()})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_callback(client, "http://cb/x", {"a": 1})
    assert len(seen) == 1 and seen[0]["body"] == '{"a":1}'


async def test_retries_on_5xx_with_backoff_then_gives_up() -> None:
    calls = 0
    slept: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_callback(client, "http://cb/x", {}, sleep=fake_sleep)
    assert calls == 3 and slept == [1, 2]


async def test_4xx_is_final_and_transport_errors_are_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(404)

    async def fake_sleep(seconds: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await deliver_callback(client, "http://cb/x", {}, sleep=fake_sleep)
    assert calls == 2
