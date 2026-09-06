from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

AUTH = {"Authorization": "Bearer test-key"}


async def _account(db: aiosqlite.Connection, vm_limit: int) -> None:
    await insert_account(db, account_row(vm_limit=vm_limit))


async def test_create_is_limited_and_destroyed_computers_do_not_count(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    await _account(db, vm_limit=2)
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/computers", json={}, headers=AUTH)
        second = await client.post("/computers", json={}, headers=AUTH)
        assert first.status_code == 200 and second.status_code == 200
        third = await client.post("/computers", json={}, headers=AUTH)
        assert third.status_code == 429 and third.json()["detail"] == "VM limit reached"
        gone = await client.delete(f"/computers/{first.json()['computer_id']}", headers=AUTH)
        assert gone.status_code == 200
        fourth = await client.post("/computers", json={}, headers=AUTH)
        assert fourth.status_code == 200
