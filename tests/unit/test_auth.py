from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config


async def _account(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row(api_key="test-key-123"))


async def test_no_auth_returns_401(db: aiosqlite.Connection, runtime_config: Config) -> None:
    await _account(db)
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/computers", json={})
    assert resp.status_code == 401


async def test_bad_key_returns_401(db: aiosqlite.Connection, runtime_config: Config) -> None:
    await _account(db)
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/computers", json={}, headers={"Authorization": "Bearer wrong-key"}
        )
    assert resp.status_code == 401


async def test_health_no_auth_required(db: aiosqlite.Connection, runtime_config: Config) -> None:
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
