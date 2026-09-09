"""The relay's two routes (#110): accepted at once, readable by its creator, and a
scoped key held to its pinned targets and wake-up."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.config import Config
from mshkn.db import insert_account
from mshkn.host.fake import FakeHost
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import aiosqlite

    from mshkn.runtime import Runtime

ACCOUNT = {"Authorization": "Bearer test-key"}
RELAY_SCOPE = {
    "relay": {
        "targets": ["https://model.example/"],
        "deliver": {"label": "brain", "exec": "membrane resume"},
    }
}


async def _public(hostname: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
async def runtime(db: aiosqlite.Connection, tmp_path: Path) -> AsyncIterator[Runtime]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"echo": request.url.path})

    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    host = FakeHost()
    rt = make_runtime(
        db, config=config, host=host, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    rt.relay.resolve = _public

    async def no_sleep(seconds: float) -> None:
        return None

    rt.relay.sleep = no_sleep  # the delivery's retries, without waiting
    try:
        yield rt
    finally:
        await rt.tasks.drain(timeout=2.0)
        await rt.http.aclose()
        host.close()


@pytest.fixture
async def client(db: aiosqlite.Connection, runtime: Runtime) -> AsyncIterator[AsyncClient]:
    await insert_account(db, account_row())
    async with AsyncClient(
        transport=ASGITransport(app=make_app(runtime)), base_url="http://test", headers=ACCOUNT
    ) as c:
        yield c


async def _key(client: AsyncClient, scopes: dict[str, Any]) -> tuple[str, dict[str, str]]:
    resp = await client.post("/keys", json={"scopes": scopes, "label": "t"}, headers=ACCOUNT)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"], {"Authorization": f"Bearer {resp.json()['secret']}"}


async def test_the_account_key_submits_polls_and_gets_the_whole_record(
    client: AsyncClient, runtime: Runtime
) -> None:
    resp = await client.post(
        "/relay",
        json={
            "target": "https://model.example/v1/messages",
            "body": {"a": 1},
            "forward_headers": {"x-api-key": "s"},
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "queued"
    job_id = resp.json()["job_id"]
    await runtime.tasks.wait(f"relay:{job_id}")
    got = await client.get(f"/relay/{job_id}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["job_id"] == job_id and body["status"] == "completed" and body["attempts"] == 1
    assert body["response"]["status"] == 200 and body["response"]["body"] == {
        "echo": "/v1/messages"
    }
    assert (
        body["response"]["headers"]["content-type"] == "application/json"
    )  # httpx adds content-length too
    assert body["delivery"] is None and body["error"] is None
    assert "forward_headers" not in got.text and "x-api-key" not in got.text
    assert (await client.get("/relay/rj-nope")).status_code == 404


async def test_bad_requests_are_refused_before_a_job_exists(client: AsyncClient) -> None:
    blocked = await client.post("/relay", json={"target": "http://127.0.0.1:8000/health"})
    assert blocked.status_code == 422 and "blocked" in blocked.json()["detail"]
    scheme = await client.post("/relay", json={"target": "ftp://model.example/"})
    assert scheme.status_code == 422
    unknown = await client.post(
        "/relay", json={"target": "https://model.example/", "timeout_ms": 5}
    )
    assert unknown.status_code == 422
    too_long = await client.post(
        "/relay", json={"target": "https://model.example/", "timeout_seconds": 999999}
    )
    assert too_long.status_code == 422 and "timeout_seconds" in too_long.json()["detail"]
    method = await client.post(
        "/relay", json={"target": "https://model.example/", "method": "TRACE"}
    )
    assert method.status_code == 422


async def test_a_scoped_key_is_held_to_its_targets_and_its_pinned_wake_up(
    client: AsyncClient, runtime: Runtime
) -> None:
    _, plain = await _key(client, {"recipes": {"read": True}})
    refused = await client.post("/relay", json={"target": "https://model.example/"}, headers=plain)
    assert refused.status_code == 403 and "relay" in refused.json()["detail"]
    _key_id, scoped = await _key(client, RELAY_SCOPE)
    outside = await client.post("/relay", json={"target": "https://other.example/"}, headers=scoped)
    assert outside.status_code == 403 and "relay.targets" in outside.json()["detail"]
    with_deliver = await client.post(
        "/relay",
        json={"target": "https://model.example/x", "deliver": {"label": "verb/x", "exec": "true"}},
        headers=scoped,
    )
    assert with_deliver.status_code == 422
    accepted = await client.post(
        "/relay", json={"target": "https://model.example/x"}, headers=scoped
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    await runtime.tasks.wait(f"relay:{job_id}")
    mine = await client.get(f"/relay/{job_id}", headers=scoped)
    assert mine.status_code == 200
    assert (
        mine.json()["delivery"]["label"] == "brain"
        and mine.json()["delivery"]["exec"] == "membrane resume"
    )
    assert mine.json()["delivery"]["status"] == "failed", "no brain chain exists here"
    _, other = await _key(client, RELAY_SCOPE)
    assert (await client.get(f"/relay/{job_id}", headers=other)).status_code == 404
    assert (await client.get(f"/relay/{job_id}")).status_code == 200, "the account sees every job"
