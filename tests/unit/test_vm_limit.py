from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, insert_computer
from mshkn.models import Account, Computer, ComputerStatus
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite


async def _account(db: aiosqlite.Connection, vm_limit: int = 2) -> None:
    await insert_account(
        db,
        Account(
            id="acct-1",
            api_key="test-key",
            vm_limit=vm_limit,
            created_at="2026-03-08T00:00:00",
        ),
    )


def _make_computer(n: int, status: ComputerStatus = ComputerStatus.RUNNING) -> Computer:
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


async def test_create_succeeds_under_limit(db: aiosqlite.Connection) -> None:
    await _account(db, vm_limit=2)
    await insert_computer(db, _make_computer(1))

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _make_computer(99)

    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )
    assert resp.status_code == 200
    vm_mgr.create.assert_called_once()


async def test_create_rejected_at_limit(db: aiosqlite.Connection) -> None:
    await _account(db, vm_limit=2)
    await insert_computer(db, _make_computer(1))
    await insert_computer(db, _make_computer(2))

    app = make_app(make_runtime(db, vm_manager=AsyncMock()))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )
    assert resp.status_code == 429
    assert resp.json()["detail"] == "VM limit reached"


async def test_destroyed_computers_dont_count_toward_limit(db: aiosqlite.Connection) -> None:
    await _account(db, vm_limit=1)
    await insert_computer(db, _make_computer(1, status=ComputerStatus.DESTROYED))

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _make_computer(99)

    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )
    assert resp.status_code == 200
    vm_mgr.create.assert_called_once()
