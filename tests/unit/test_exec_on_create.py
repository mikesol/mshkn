"""Tests for exec-on-create and exec-on-fork features (issue #31)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, insert_checkpoint, insert_computer, run_migrations
from mshkn.main import app
from mshkn.models import Account, Checkpoint, Computer
from mshkn.vm.ssh import ExecResult


async def _setup(tmp_path: Path) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(
        db,
        Account(
            id="acct-1",
            api_key="test-key",
            vm_limit=10,
            created_at="2026-03-08T00:00:00",
        ),
    )
    return db


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
        status="running",
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
    )


AUTH = {"Authorization": "Bearer test-key"}


async def test_create_without_exec_works_as_before(tmp_path: Path) -> None:
    """Create without exec field returns no exec results."""
    db = await _setup(tmp_path)
    mock_computer = _make_computer()
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(domain="test.dev")
    app.state.vm_manager = vm_mgr

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"uses": []}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"] == "comp-1"
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
    await db.close()


async def test_create_with_exec_returns_results(tmp_path: Path) -> None:
    """Create with exec runs the command and returns results."""
    db = await _setup(tmp_path)
    mock_computer = _make_computer()
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(
        domain="test.dev",
        ssh_key_path=Path("/tmp/fake-key"),
    )
    app.state.vm_manager = vm_mgr

    mock_result = ExecResult(exit_code=0, stdout="hello world\n", stderr="")

    transport = ASGITransport(app=app)
    with patch("mshkn.api.computers.ssh_exec", return_value=mock_result) as mock_ssh:
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
    mock_ssh.assert_called_once()
    await db.close()


async def test_create_with_exec_nonzero_exit(tmp_path: Path) -> None:
    """Create with exec that fails still returns the result (not an error)."""
    db = await _setup(tmp_path)
    mock_computer = _make_computer()
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(
        domain="test.dev",
        ssh_key_path=Path("/tmp/fake-key"),
    )
    app.state.vm_manager = vm_mgr

    mock_result = ExecResult(exit_code=1, stdout="", stderr="command not found\n")

    transport = ASGITransport(app=app)
    with patch("mshkn.api.computers.ssh_exec", return_value=mock_result):
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
    await db.close()


async def test_fork_with_exec_returns_results(tmp_path: Path) -> None:
    """Fork with exec runs the command after restore and returns results."""
    db = await _setup(tmp_path)

    # Insert a checkpoint to fork from
    ckpt = Checkpoint(
        id="ckpt-test1",
        account_id="acct-1",
        parent_id=None,
        computer_id="comp-orig",
        thin_volume_id=1,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        r2_prefix="acct-1/ckpt-test1",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )
    await insert_checkpoint(db, ckpt)

    mock_computer = _make_computer(2)
    vm_mgr = AsyncMock()
    vm_mgr.fork_from_checkpoint.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(
        domain="test.dev",
        ssh_key_path=Path("/tmp/fake-key"),
    )
    app.state.vm_manager = vm_mgr

    mock_result = ExecResult(exit_code=0, stdout="forked output\n", stderr="")

    transport = ASGITransport(app=app)
    with patch("mshkn.vm.ssh.ssh_exec", return_value=mock_result) as mock_ssh:
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
    mock_ssh.assert_called_once()
    await db.close()


async def test_fork_without_exec_works_as_before(tmp_path: Path) -> None:
    """Fork without exec returns no exec results."""
    db = await _setup(tmp_path)

    ckpt = Checkpoint(
        id="ckpt-test2",
        account_id="acct-1",
        parent_id=None,
        computer_id="comp-orig",
        thin_volume_id=1,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        r2_prefix="acct-1/ckpt-test2",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )
    await insert_checkpoint(db, ckpt)

    mock_computer = _make_computer(3)
    vm_mgr = AsyncMock()
    vm_mgr.fork_from_checkpoint.return_value = mock_computer

    app.state.db = db
    app.state.config = MagicMock(domain="test.dev")
    app.state.vm_manager = vm_mgr

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
    await db.close()
