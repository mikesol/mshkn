"""Tests for self-destruct (auto-checkpoint + destroy) and callback URL features."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from mshkn.config import Config
from mshkn.db import (
    insert_account,
    insert_checkpoint,
    insert_computer,
    list_checkpoints_by_account,
)
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Checkpoint, Computer, ComputerStatus
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

AUTH = {"Authorization": "Bearer test-key"}


def _config(tmp_path: Path) -> Config:
    """A Config whose checkpoint dir is writable: the fake hypervisor writes
    real snapshot files there and the fake object store uploads them."""
    return Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")


def _account() -> Account:
    return Account(
        id="acct-1",
        api_key="test-key",
        vm_limit=10,
        created_at="2026-03-08T00:00:00",
    )


def _computer(n: int = 1, status: ComputerStatus = ComputerStatus.RUNNING) -> Computer:
    return Computer(
        id=f"comp-{n}",
        account_id="acct-1",
        thin_volume_id=n,
        tap_device=f"tap{n}",
        vm_ip=f"172.16.1.{n + 1}",
        socket_path=f"/tmp/fc-comp-{n}.socket",
        firecracker_pid=1000 + n,
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
        r2_prefix="acct-1/ckpt-1",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=label,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )


async def test_create_with_exec_returns_exec_result(db: aiosqlite.Connection) -> None:
    """Create with exec runs the command and returns the result."""
    await insert_account(db, _account())
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _computer()

    host = FakeHost()
    host.guest.script["echo hello"] = ExecResult(exit_code=0, stdout="hello\n", stderr="")
    app = make_app(make_runtime(db, vm_manager=vm_mgr, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": [], "exec": "echo hello"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "hello\n"
    assert data["exec_stderr"] == ""
    assert data["created_checkpoint_id"] is None  # no self_destruct
    assert host.guest.commands == [("172.16.1.2", "echo hello")]
    assert host.objects.prefixes == {}  # nothing checkpointed


async def test_create_without_exec_returns_none_fields(db: aiosqlite.Connection) -> None:
    """Create without exec returns null exec fields."""
    await insert_account(db, _account())
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _computer()

    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"uses": []}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
    assert data["created_checkpoint_id"] is None


async def test_self_destruct_creates_checkpoint_and_destroys(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """Self-destruct creates a checkpoint, destroys computer, and returns checkpoint ID."""
    await insert_account(db, _account())
    computer = _computer()
    await insert_computer(db, computer)

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    host = FakeHost()
    host.guest.script["echo done"] = ExecResult(exit_code=0, stdout="done\n", stderr="")
    rt = make_runtime(db, vm_manager=vm_mgr, host=host, config=_config(tmp_path))
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={
                "uses": [],
                "exec": "echo done",
                "self_destruct": True,
                "label": "test-chain",
            },
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    checkpoint_id = data["created_checkpoint_id"]
    assert checkpoint_id is not None
    assert checkpoint_id.startswith("ckpt-")

    # Verify checkpoint was inserted in DB
    checkpoints = await list_checkpoints_by_account(db, "acct-1")
    assert len(checkpoints) == 1
    assert checkpoints[0].label == "test-chain"

    # The guest was flushed, the VM snapshotted, and the pooled connection evicted
    assert host.guest.commands == [("172.16.1.2", "echo done"), ("172.16.1.2", "sync")]
    assert host.hypervisor.snapshots == [(computer.socket_path, tmp_path / "ckpts" / checkpoint_id)]
    assert host.guest.evicted == ["172.16.1.2"]

    # The snapshot files were uploaded to R2 under the checkpoint's prefix
    await rt.tasks.wait(f"upload:{checkpoint_id}")
    assert sorted(host.objects.prefixes[f"acct-1/{checkpoint_id}"]) == ["memory", "vmstate"]

    # Verify destroy was called
    vm_mgr.destroy.assert_called_once_with(computer.id)


async def test_self_destruct_without_exec_is_noop(db: aiosqlite.Connection) -> None:
    """Self-destruct without exec does nothing (no completion event)."""
    await insert_account(db, _account())
    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = _computer()

    app = make_app(make_runtime(db, vm_manager=vm_mgr))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": [], "self_destruct": True},  # no exec!
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["created_checkpoint_id"] is None
    # destroy should NOT have been called
    vm_mgr.destroy.assert_not_called()


async def test_self_destruct_on_nonzero_exit(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """Self-destruct fires even on non-zero exit (preserves error state)."""
    await insert_account(db, _account())
    computer = _computer()
    await insert_computer(db, computer)

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    host = FakeHost()
    host.guest.script["false"] = ExecResult(exit_code=1, stdout="", stderr="error!\n")
    rt = make_runtime(db, vm_manager=vm_mgr, host=host, config=_config(tmp_path))
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"uses": [], "exec": "false", "self_destruct": True},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 1
    assert data["exec_stderr"] == "error!\n"
    checkpoint_id = data["created_checkpoint_id"]
    assert checkpoint_id is not None
    await rt.tasks.wait(f"upload:{checkpoint_id}")
    assert f"acct-1/{checkpoint_id}" in host.objects.prefixes
    vm_mgr.destroy.assert_called_once()


async def test_fork_with_exec_and_self_destruct(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """Fork with exec + self_destruct creates checkpoint, destroys, returns results."""
    await insert_account(db, _account())
    await insert_checkpoint(db, _checkpoint(ckpt_id="ckpt-source", label="my-chain"))

    forked_computer = _computer(n=2)
    forked_computer.source_checkpoint_id = "ckpt-source"
    await insert_computer(db, forked_computer)

    vm_mgr = AsyncMock()
    vm_mgr.fork_from_checkpoint.return_value = forked_computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    host = FakeHost()
    host.guest.script["echo forked"] = ExecResult(exit_code=0, stdout="forked\n", stderr="")
    rt = make_runtime(db, vm_manager=vm_mgr, host=host, config=_config(tmp_path))
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/checkpoints/ckpt-source/fork",
            json={"exec": "echo forked", "self_destruct": True},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "forked\n"
    checkpoint_id = data["created_checkpoint_id"]
    assert checkpoint_id is not None

    # The fork exec and the self-destruct sync both went to the forked VM
    assert host.guest.commands == [("172.16.1.3", "echo forked"), ("172.16.1.3", "sync")]
    await rt.tasks.wait(f"upload:{checkpoint_id}")
    assert f"acct-1/{checkpoint_id}" in host.objects.prefixes

    # Verify checkpoint inherits label from source
    checkpoints = await list_checkpoints_by_account(db, "acct-1")
    # Should have source + new checkpoint
    new_ckpts = [c for c in checkpoints if c.id != "ckpt-source"]
    assert len(new_ckpts) == 1
    assert new_ckpts[0].label == "my-chain"  # inherited from source

    vm_mgr.destroy.assert_called_once()


async def test_callback_url_fires_on_self_destruct(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """Callback URL receives correct payload on self-destruct."""
    await insert_account(db, _account())
    computer = _computer()
    await insert_computer(db, computer)

    vm_mgr = AsyncMock()
    vm_mgr.create.return_value = computer
    vm_mgr.snapshot_disk_for_checkpoint.return_value = 99
    vm_mgr.destroy.return_value = None

    host = FakeHost()
    host.guest.script["echo out"] = ExecResult(exit_code=0, stdout="out\n", stderr="err\n")
    app = make_app(make_runtime(db, vm_manager=vm_mgr, host=host, config=_config(tmp_path)))

    captured_payload: dict[str, Any] | None = None

    async def fake_deliver(url: str, payload: dict[str, Any], max_retries: int = 3) -> None:
        nonlocal captured_payload
        captured_payload = payload

    with patch("mshkn.api.computers.deliver_callback", side_effect=fake_deliver):
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
                headers=AUTH,
            )

        assert resp.status_code == 200

        # Wait for the background callback task to complete
        await asyncio.sleep(0.1)

    assert captured_payload is not None
    assert captured_payload["computer_id"] == computer.id
    assert captured_payload["checkpoint_id"] is None  # no source checkpoint for create
    assert captured_payload["label"] == "test-label"
    assert captured_payload["exec_exit_code"] == 0
    assert captured_payload["exec_stdout"] == "out\n"
    assert captured_payload["exec_stderr"] == "err\n"
    assert captured_payload["created_checkpoint_id"].startswith("ckpt-")
