from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, insert_computer, run_migrations
from mshkn.main import app
from mshkn.models import Account, Computer


async def _setup(tmp_path: Path, vm_limit: int = 2) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(
        db,
        Account(
            id="acct-1",
            api_key="test-key",
            vm_limit=vm_limit,
            created_at="2026-03-08T00:00:00",
        ),
    )
    return db


def _make_computer(n: int, status: str = "running") -> Computer:
    return Computer(
        id=f"comp-{n}",
        account_id="acct-1",
        thin_volume_id=n,
        tap_device=f"tap{n}",
        vm_ip=f"172.16.1.{n + 1}",
        socket_path=f"/tmp/fc-{n}.socket",
        firecracker_pid=1000 + n,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        status=status,
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
    )


async def test_create_succeeds_under_limit(tmp_path: Path) -> None:
    db = await _setup(tmp_path, vm_limit=2)
    await insert_computer(db, _make_computer(1))

    mock_computer = _make_computer(99)
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(domain="test.dev")
    app.state.vm_manager = vm_mgr

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )
    assert resp.status_code == 200
    vm_mgr.create.assert_called_once()
    await db.close()


async def test_create_rejected_at_limit(tmp_path: Path) -> None:
    db = await _setup(tmp_path, vm_limit=2)
    await insert_computer(db, _make_computer(1))
    await insert_computer(db, _make_computer(2))

    app.state.db = db
    app.state.config = MagicMock(domain="test.dev")
    app.state.vm_manager = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )
    assert resp.status_code == 429
    assert resp.json()["detail"] == "VM limit reached"
    await db.close()


async def test_destroyed_computers_dont_count_toward_limit(tmp_path: Path) -> None:
    db = await _setup(tmp_path, vm_limit=1)
    await insert_computer(db, _make_computer(1, status="destroyed"))

    mock_computer = _make_computer(99)
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(domain="test.dev")
    app.state.vm_manager = vm_mgr

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )
    assert resp.status_code == 200
    vm_mgr.create.assert_called_once()
    await db.close()
