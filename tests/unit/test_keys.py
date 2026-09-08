"""Scoped keys (#88): the bearer resolves to a principal, and /keys is the
account key's management surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.db import get_api_key, insert_account
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from mshkn.config import Config

ACCOUNT = {"Authorization": "Bearer test-key"}
BRAIN = {
    "recipes": {"create": True, "read": True},
    "computers": {"create_from": "*"},
    "labels": ["verb/"],
}


@pytest.fixture
async def client(db: aiosqlite.Connection, runtime_config: Config) -> AsyncIterator[AsyncClient]:
    await insert_account(db, account_row())
    await insert_account(db, account_row("acct-2", api_key="other-key"))
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _scoped(
    client: AsyncClient, scopes: dict[str, object], label: str = "brain"
) -> dict[str, Any]:
    resp = await client.post("/keys", json={"scopes": scopes, "label": label}, headers=ACCOUNT)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def test_create_returns_the_secret_once(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    created = await _scoped(client, BRAIN)
    assert set(created) == {"id", "secret", "label", "scopes", "created_at"}
    assert created["id"].startswith("key-") and created["secret"].startswith("mk-")
    assert created["scopes"] == BRAIN and created["label"] == "brain"
    listed = (await client.get("/keys", headers=ACCOUNT)).json()
    assert listed == [
        {
            "id": created["id"],
            "label": "brain",
            "scopes": BRAIN,
            "created_at": created["created_at"],
        }
    ]
    stored = await get_api_key(db, created["id"])
    assert stored is not None and stored.account_id == "acct-1"


async def test_scopes_document_is_validated(client: AsyncClient) -> None:
    resp = await client.post("/keys", json={"scopes": {"pets": True}}, headers=ACCOUNT)
    assert resp.status_code == 422
    assert "unknown fields ['pets']" in resp.json()["detail"]


async def test_scoped_key_authenticates_and_an_unknown_one_does_not(client: AsyncClient) -> None:
    created = await _scoped(client, BRAIN)
    scoped = {"Authorization": f"Bearer {created['secret']}"}
    assert (await client.get("/recipes", headers=scoped)).status_code == 200
    unknown = {"Authorization": "Bearer mk-x"}
    assert (await client.get("/recipes", headers=unknown)).status_code == 401


async def test_keys_are_per_account_and_deleted_keys_stop_working(client: AsyncClient) -> None:
    created = await _scoped(client, BRAIN)
    other = {"Authorization": "Bearer other-key"}
    assert (await client.get("/keys", headers=other)).json() == []
    assert (await client.delete(f"/keys/{created['id']}", headers=other)).status_code == 404
    assert (await client.delete(f"/keys/{created['id']}", headers=ACCOUNT)).status_code == 200
    assert (await client.delete(f"/keys/{created['id']}", headers=ACCOUNT)).status_code == 404
    assert (await client.get("/keys", headers=ACCOUNT)).json() == []
    scoped = {"Authorization": f"Bearer {created['secret']}"}
    assert (await client.get("/recipes", headers=scoped)).status_code == 401


async def test_a_scoped_key_cannot_manage_keys(client: AsyncClient) -> None:
    created = await _scoped(client, BRAIN)
    scoped = {"Authorization": f"Bearer {created['secret']}"}
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", "/keys", {"scopes": {}}),
        ("GET", "/keys", None),
        ("DELETE", f"/keys/{created['id']}", None),
    ]
    for method, url, body in calls:
        resp = await client.request(method, url, json=body, headers=scoped)
        assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}: {resp.text}"
        assert "account key" in resp.json()["detail"]
    assert (await client.get("/keys", headers=ACCOUNT)).json() != []
