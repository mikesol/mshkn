"""Tests for self-destruct (auto-checkpoint + destroy) and callback URL features."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from mshkn.db import (
    get_computer,
    insert_account,
    list_checkpoints_by_account,
)
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, CheckpointTrigger, ComputerStatus
from mshkn.resources import DEFAULT_RESOURCES
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Checkpoint
    from mshkn.runtime import Runtime

AUTH = {"Authorization": "Bearer test-key"}

ACCOUNT = Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-03-08T00:00:00")


def _receiver() -> tuple[FastAPI, list[dict[str, Any]]]:
    app = FastAPI()
    received: list[dict[str, Any]] = []

    @app.post("/cb")
    async def cb(request: Request) -> dict[str, str]:
        received.append(await request.json())
        return {"ok": "yes"}

    return app, received


async def _labelled_checkpoint(rt: Runtime, label: str) -> Checkpoint:
    computer = await rt.computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    return await rt.checkpoints.create(computer, label=label, trigger=CheckpointTrigger.API)


async def _vm_ip(db: aiosqlite.Connection, computer_id: str) -> str:
    computer = await get_computer(db, computer_id)
    assert computer is not None
    return computer.vm_ip


async def _status(db: aiosqlite.Connection, computer_id: str) -> ComputerStatus:
    computer = await get_computer(db, computer_id)
    assert computer is not None
    return computer.status


async def test_create_with_exec_returns_exec_result(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Create with exec runs the command and returns the result."""
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    host.guest.script["echo hello"] = ExecResult(exit_code=0, stdout="hello\n", stderr="")
    app = make_app(make_runtime(db, config=runtime_config, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"exec": "echo hello"}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "hello\n"
    assert data["exec_stderr"] == ""
    assert data["created_checkpoint_id"] is None  # no self_destruct
    assert host.guest.commands == [(await _vm_ip(db, data["computer_id"]), "echo hello")]
    assert host.objects.prefixes == {}  # nothing checkpointed
    assert await _status(db, data["computer_id"]) is ComputerStatus.RUNNING


async def test_create_without_exec_returns_none_fields(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Create without exec returns null exec fields."""
    await insert_account(db, ACCOUNT)
    app = make_app(make_runtime(db, config=runtime_config))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
    assert data["created_checkpoint_id"] is None


async def test_self_destruct_creates_checkpoint_and_destroys(
    db: aiosqlite.Connection, tmp_path: Path, runtime_config: Config
) -> None:
    """Self-destruct creates a checkpoint, destroys computer, and returns checkpoint ID."""
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    host.guest.script["echo done"] = ExecResult(exit_code=0, stdout="done\n", stderr="")
    rt = make_runtime(db, config=runtime_config, host=host)
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"exec": "echo done", "self_destruct": True, "label": "test-chain"},
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
    computer = await get_computer(db, data["computer_id"])
    assert computer is not None
    ip = computer.vm_ip
    assert host.guest.commands == [(ip, "echo done"), (ip, "sync")]
    assert host.hypervisor.snapshots[-1] == (
        computer.socket_path,
        tmp_path / "ckpts" / checkpoint_id,
    )
    # once for the checkpoint's pause/resume, once when the computer is destroyed
    assert host.guest.evicted == [ip, ip]

    # The snapshot files were uploaded to R2 under the checkpoint's prefix
    await rt.tasks.wait(f"upload:{checkpoint_id}")
    assert sorted(host.objects.prefixes[f"acct-1/{checkpoint_id}"]) == ["memory", "vmstate"]

    # The computer really is gone: destroyed row, no VM, no route, no volume
    assert computer.status is ComputerStatus.DESTROYED
    assert host.hypervisor.alive == {}
    assert host.proxy.routes == {}
    assert computer.thin_volume_id not in host.blocks.volumes
    await rt.tasks.drain(timeout=2.0)


async def test_self_destruct_without_exec_is_noop(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Self-destruct without exec does nothing (no completion event)."""
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    app = make_app(make_runtime(db, config=runtime_config, host=host))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"self_destruct": True},  # no exec!
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["created_checkpoint_id"] is None
    # the computer was NOT destroyed
    assert await _status(db, data["computer_id"]) is ComputerStatus.RUNNING
    assert await list_checkpoints_by_account(db, "acct-1") == []


async def test_self_destruct_on_nonzero_exit(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Self-destruct fires even on non-zero exit (preserves error state)."""
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    host.guest.script["false"] = ExecResult(exit_code=1, stdout="", stderr="error!\n")
    rt = make_runtime(db, config=runtime_config, host=host)
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"exec": "false", "self_destruct": True},
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
    assert await _status(db, data["computer_id"]) is ComputerStatus.DESTROYED


async def test_fork_with_exec_and_self_destruct(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Fork with exec + self_destruct creates checkpoint, destroys, returns results."""
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    host.guest.script["echo forked"] = ExecResult(exit_code=0, stdout="forked\n", stderr="")
    rt = make_runtime(db, config=runtime_config, host=host)
    source = await _labelled_checkpoint(rt, "my-chain")
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/checkpoints/{source.id}/fork",
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
    fork_ip = await _vm_ip(db, data["computer_id"])
    assert host.guest.commands[-2:] == [(fork_ip, "echo forked"), (fork_ip, "sync")]
    await rt.tasks.wait(f"upload:{checkpoint_id}")
    assert f"acct-1/{checkpoint_id}" in host.objects.prefixes

    # Verify checkpoint inherits label from source
    checkpoints = await list_checkpoints_by_account(db, "acct-1")
    new_ckpts = [c for c in checkpoints if c.id != source.id]
    assert len(new_ckpts) == 1
    assert new_ckpts[0].label == "my-chain"  # inherited from source
    assert new_ckpts[0].parent_id == source.id

    assert await _status(db, data["computer_id"]) is ComputerStatus.DESTROYED
    await rt.tasks.drain(timeout=2.0)


async def test_callback_url_fires_on_self_destruct(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Callback URL receives correct payload on self-destruct."""
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    host.guest.script["echo out"] = ExecResult(exit_code=0, stdout="out\n", stderr="err\n")
    receiver, received = _receiver()
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver), base_url="http://receiver"
    )
    rt = make_runtime(db, config=runtime_config, host=host, http=http)
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={
                "exec": "echo out",
                "self_destruct": True,
                "callback_url": "http://receiver/cb",
                "label": "test-label",
            },
            headers=AUTH,
        )

    assert resp.status_code == 200
    await rt.tasks.drain(timeout=2.0)
    await http.aclose()

    assert len(received) == 1
    payload = received[0]
    assert payload["computer_id"] == resp.json()["computer_id"]
    assert payload["checkpoint_id"] is None  # no source checkpoint for create
    assert payload["label"] == "test-label"
    assert payload["exec_exit_code"] == 0
    assert payload["exec_stdout"] == "out\n"
    assert payload["exec_stderr"] == "err\n"
    assert payload["created_checkpoint_id"].startswith("ckpt-")
