"""Tests for exec-on-create and exec-on-fork features (issue #31)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, insert_checkpoint
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Checkpoint, Computer, ComputerStatus
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite


async def _account(db: aiosqlite.Connection) -> None:
    await insert_account(
        db,
        Account(
            id="acct-1",
            api_key="test-key",
            vm_limit=10,
            created_at="2026-03-08T00:00:00",
        ),
    )


def _make_computer(n: int = 1) -> Computer:
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
        status=ComputerStatus.RUNNING,
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
    )


def _make_checkpoint(ckpt_id: str) -> Checkpoint:
    return Checkpoint(
        id=ckpt_id,
        account_id="acct-1",
        parent_id=None,
        computer_id="comp-orig",
        thin_volume_id=1,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        r2_prefix=f"acct-1/{ckpt_id}",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )


AUTH = {"Authorization": "Bearer test-key"}


async def test_create_without_exec_works_as_before(db: aiosqlite.Connection) -> None:
    """Create without exec field returns no exec results."""
    await _account(db)
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _make_computer()

    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"uses": []}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"] == "comp-1"
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None


async def test_create_with_exec_returns_results(db: aiosqlite.Connection) -> None:
    """Create with exec runs the command and returns results."""
    await _account(db)
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _make_computer()

    host = FakeHost()
    host.guest.script["echo hello world"] = ExecResult(
        exit_code=0, stdout="hello world\n", stderr=""
    )
    app = make_app(make_runtime(db, vm_manager=vm_mgr, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": [], "exec": "echo hello world"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"] == "comp-1"
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "hello world\n"
    assert data["exec_stderr"] == ""
    assert host.guest.commands == [("172.16.1.2", "echo hello world")]


async def test_create_with_exec_nonzero_exit(db: aiosqlite.Connection) -> None:
    """Create with exec that fails still returns the result (not an error)."""
    await _account(db)
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _make_computer()

    host = FakeHost()
    host.guest.script["bad-command"] = ExecResult(
        exit_code=1, stdout="", stderr="command not found\n"
    )
    app = make_app(make_runtime(db, vm_manager=vm_mgr, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": [], "exec": "bad-command"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 1
    assert data["exec_stderr"] == "command not found\n"
    assert host.guest.commands == [("172.16.1.2", "bad-command")]


async def test_fork_with_exec_returns_results(db: aiosqlite.Connection) -> None:
    """Fork with exec runs the command after restore and returns results."""
    await _account(db)
    await insert_checkpoint(db, _make_checkpoint("ckpt-test1"))

    vm_mgr = AsyncMock()
    vm_mgr.fork_from_checkpoint.return_value = _make_computer(2)

    host = FakeHost()
    host.guest.script["echo forked output"] = ExecResult(
        exit_code=0, stdout="forked output\n", stderr=""
    )
    app = make_app(make_runtime(db, vm_manager=vm_mgr, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/checkpoints/ckpt-test1/fork",
            json={"exec": "echo forked output"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"] == "comp-2"
    assert data["checkpoint_id"] == "ckpt-test1"
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "forked output\n"
    assert data["exec_stderr"] == ""
    assert host.guest.commands == [("172.16.1.3", "echo forked output")]


async def test_fork_without_exec_works_as_before(db: aiosqlite.Connection) -> None:
    """Fork without exec returns no exec results."""
    await _account(db)
    await insert_checkpoint(db, _make_checkpoint("ckpt-test2"))

    vm_mgr = AsyncMock()
    vm_mgr.fork_from_checkpoint.return_value = _make_computer(3)

    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/checkpoints/ckpt-test2/fork",
            json={},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"] == "comp-3"
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
