from pathlib import Path

import aiosqlite
from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, run_migrations
from mshkn.main import app
from mshkn.models import Account


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(
        db,
        Account(
            id="acct-1",
            api_key="test-key-123",
            vm_limit=10,
            created_at="2026-03-08T00:00:00",
        ),
    )
    return db


async def test_no_auth_returns_401(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"uses": []})
    assert resp.status_code == 401
    await db.close()


async def test_bad_key_returns_401(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    app.state.db = db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert resp.status_code == 401
    await db.close()


async def test_health_no_auth_required() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
