"""Tests for self-destruct (auto-checkpoint + destroy) and callback URL features."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from httpx import ASGITransport, AsyncClient

from mshkn.db import (
    insert_account,
    insert_checkpoint,
    insert_computer,
    list_checkpoints_by_account,
    run_migrations,
)
from mshkn.main import app
from mshkn.models import Account, Checkpoint, Computer
from mshkn.vm.ssh import ExecResult


def _account() -> Account:
    return Account(
        id="acct-1",
        api_key="test-key",
        vm_limit=10,
        created_at="2026-03-08T00:00:00",
    )


def _computer(n: int = 1, status: str = "running") -> Computer:
    return Computer(
        id=f"comp-{n}",
        account_id="acct-1",
        thin_volume_id=n,
        tap_device=f"tap{n}",
        vm_ip=f"172.16.1.{n + 1}",
        socket_path=f"/tmp/fc-comp-{n}.socket",
        firecracker_pid=1000 + n,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        status=status,
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
    )


def _checkpoint(
    ckpt_id: str = "ckpt-1",
    computer_id: str | None = "comp-1",
    label: str | None = "my-chain",
    parent_id: str | None = None,
) -> Checkpoint:
    return Checkpoint(
        id=ckpt_id,
        account_id="acct-1",
        parent_id=parent_id,
        computer_id=computer_id,
        thin_volume_id=50,
        manifest_hash="abc",
        manifest_json='{"uses": []}',
        r2_prefix="acct-1/ckpt-1",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=label,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )


async def _setup(tmp_path: Path) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(db, _account())
    return db


def _mock_config() -> MagicMock:
    cfg = MagicMock()
    cfg.domain = "test.dev"
    cfg.ssh_key_path = Path("/tmp/test-key")
    cfg.checkpoint_local_dir = Path("/tmp/test-ckpts")
    cfg.thin_pool_name = "test-pool"
    cfg.thin_volume_sectors = 16777216
    cfg.r2_bucket = "test-bucket"
    return cfg


async def test_create_with_exec_returns_exec_result(tmp_path: Path) -> None:
    """Create with exec runs the command and returns the result."""
    db = await _setup(tmp_path)
    computer = _computer()

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer

    app.state.db = db
    app.state.config = _mock_config()
    app.state.vm_manager = vm_mgr

    with patch("mshkn.api.computers.ssh_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = ExecResult(exit_code=0, stdout="hello\n", stderr="")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/computers",
                json={"uses": [], "exec": "echo hello"},
                headers={"Authorization": "Bearer test-key"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "hello\n"
    assert data["exec_stderr"] == ""
    assert data["created_checkpoint_id"] is None  # no self_destruct
    await db.close()


async def test_create_without_exec_returns_none_fields(tmp_path: Path) -> None:
    """Create without exec returns null exec fields."""
    db = await _setup(tmp_path)
    computer = _computer()

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer

    app.state.db = db
    app.state.config = _mock_config()
    app.state.vm_manager = vm_mgr

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": []},
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
    assert data["created_checkpoint_id"] is None
    await db.close()


async def test_self_destruct_creates_checkpoint_and_destroys(tmp_path: Path) -> None:
    """Self-destruct creates a checkpoint, destroys computer, and returns checkpoint ID."""
    db = await _setup(tmp_path)
    computer = _computer()
    await insert_computer(db, computer)

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    app.state.db = db
    app.state.config = _mock_config()
    app.state.vm_manager = vm_mgr
    app.state.ssh_pool = None

    with (
        patch("mshkn.api.computers.ssh_exec", new_callable=AsyncMock) as mock_exec,
        patch("mshkn.api.computers.create_vm_snapshot", new_callable=AsyncMock),
        patch("mshkn.api.computers.upload_checkpoint", new_callable=AsyncMock),
    ):
        mock_exec.return_value = ExecResult(exit_code=0, stdout="done\n", stderr="")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/computers",
                json={"uses": [], "exec": "echo done", "self_destruct": True, "label": "test-chain"},
                headers={"Authorization": "Bearer test-key"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["created_checkpoint_id"] is not None
    assert data["created_checkpoint_id"].startswith("ckpt-")

    # Verify checkpoint was inserted in DB
    checkpoints = await list_checkpoints_by_account(db, "acct-1")
    assert len(checkpoints) == 1
    assert checkpoints[0].label == "test-chain"

    # Verify destroy was called
    vm_mgr.destroy.assert_called_once_with(computer.id)
    await db.close()


async def test_self_destruct_without_exec_is_noop(tmp_path: Path) -> None:
    """Self-destruct without exec does nothing (no completion event)."""
    db = await _setup(tmp_path)
    computer = _computer()

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer

    app.state.db = db
    app.state.config = _mock_config()
    app.state.vm_manager = vm_mgr

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": [], "self_destruct": True},  # no exec!
            headers={"Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["created_checkpoint_id"] is None
    # destroy should NOT have been called
    vm_mgr.destroy.assert_not_called()
    await db.close()


async def test_self_destruct_on_nonzero_exit(tmp_path: Path) -> None:
    """Self-destruct fires even on non-zero exit (preserves error state)."""
    db = await _setup(tmp_path)
    computer = _computer()
    await insert_computer(db, computer)

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    app.state.db = db
    app.state.config = _mock_config()
    app.state.vm_manager = vm_mgr
    app.state.ssh_pool = None

    with (
        patch("mshkn.api.computers.ssh_exec", new_callable=AsyncMock) as mock_exec,
        patch("mshkn.api.computers.create_vm_snapshot", new_callable=AsyncMock),
        patch("mshkn.api.computers.upload_checkpoint", new_callable=AsyncMock),
    ):
        mock_exec.return_value = ExecResult(exit_code=1, stdout="", stderr="error!\n")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/computers",
                json={"uses": [], "exec": "false", "self_destruct": True},
                headers={"Authorization": "Bearer test-key"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 1
    assert data["exec_stderr"] == "error!\n"
    assert data["created_checkpoint_id"] is not None
    vm_mgr.destroy.assert_called_once()
    await db.close()


async def test_fork_with_exec_and_self_destruct(tmp_path: Path) -> None:
    """Fork with exec + self_destruct creates checkpoint, destroys, returns results."""
    db = await _setup(tmp_path)
    source_ckpt = _checkpoint(ckpt_id="ckpt-source", label="my-chain")
    await insert_checkpoint(db, source_ckpt)

    forked_computer = _computer(n=2)
    forked_computer.source_checkpoint_id = "ckpt-source"
    await insert_computer(db, forked_computer)

    vm_mgr = AsyncMock()
    vm_mgr.fork_from_checkpoint.return_value = forked_computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    cfg = _mock_config()
    app.state.db = db
    app.state.config = cfg
    app.state.vm_manager = vm_mgr
    app.state.ssh_pool = None

    exec_result = ExecResult(exit_code=0, stdout="forked\n", stderr="")

    with (
        patch("mshkn.vm.ssh.ssh_exec", new_callable=AsyncMock) as mock_ssh,
        patch("mshkn.api.computers.ssh_exec", new_callable=AsyncMock) as mock_exec_comp,
        patch("mshkn.api.computers.create_vm_snapshot", new_callable=AsyncMock),
        patch("mshkn.api.computers.upload_checkpoint", new_callable=AsyncMock),
    ):
        # ssh_exec is imported locally in checkpoints.py from mshkn.vm.ssh
        mock_ssh.return_value = exec_result
        # ssh_exec is imported at module level in computers.py (used by _self_destruct for sync)
        mock_exec_comp.return_value = ExecResult(exit_code=0, stdout="", stderr="")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/checkpoints/ckpt-source/fork",
                json={"exec": "echo forked", "self_destruct": True},
                headers={"Authorization": "Bearer test-key"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "forked\n"
    assert data["created_checkpoint_id"] is not None

    # Verify checkpoint inherits label from source
    checkpoints = await list_checkpoints_by_account(db, "acct-1")
    # Should have source + new checkpoint
    new_ckpts = [c for c in checkpoints if c.id != "ckpt-source"]
    assert len(new_ckpts) == 1
    assert new_ckpts[0].label == "my-chain"  # inherited from source

    vm_mgr.destroy.assert_called_once()
    await db.close()


async def test_callback_url_fires_on_self_destruct(tmp_path: Path) -> None:
    """Callback URL receives correct payload on self-destruct."""
    db = await _setup(tmp_path)
    computer = _computer()
    await insert_computer(db, computer)

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    app.state.db = db
    app.state.config = _mock_config()
    app.state.vm_manager = vm_mgr
    app.state.ssh_pool = None

    captured_payload: dict | None = None

    async def fake_deliver(url: str, payload: dict, max_retries: int = 3) -> None:  # noqa: ARG001
        nonlocal captured_payload
        captured_payload = payload

    with (
        patch("mshkn.api.computers.ssh_exec", new_callable=AsyncMock) as mock_exec,
        patch("mshkn.api.computers.create_vm_snapshot", new_callable=AsyncMock),
        patch("mshkn.api.computers.upload_checkpoint", new_callable=AsyncMock),
        patch("mshkn.api.computers.deliver_callback", side_effect=fake_deliver),
    ):
        mock_exec.return_value = ExecResult(exit_code=0, stdout="out\n", stderr="err\n")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/computers",
                json={
                    "uses": [],
                    "exec": "echo out",
                    "self_destruct": True,
                    "callback_url": "http://example.com/cb",
                    "label": "test-label",
                },
                headers={"Authorization": "Bearer test-key"},
            )

    assert resp.status_code == 200

    # Wait for background task to complete
    import asyncio
    await asyncio.sleep(0.1)

    assert captured_payload is not None
    assert captured_payload["computer_id"] == computer.id
    assert captured_payload["checkpoint_id"] is None  # no source checkpoint for create
    assert captured_payload["label"] == "test-label"
    assert captured_payload["exec_exit_code"] == 0
    assert captured_payload["exec_stdout"] == "out\n"
    assert captured_payload["exec_stderr"] == "err\n"
    assert captured_payload["created_checkpoint_id"].startswith("ckpt-")
    await db.close()
