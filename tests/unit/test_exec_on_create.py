"""Tests for exec-on-create and exec-on-fork features (issue #31)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import get_computer, insert_account
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import Account, CheckpointTrigger
from mshkn.resources import DEFAULT_RESOURCES
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Checkpoint
    from mshkn.runtime import Runtime

AUTH = {"Authorization": "Bearer test-key"}

ACCOUNT = Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-03-08T00:00:00")


async def _account(db: aiosqlite.Connection) -> None:
    await insert_account(db, ACCOUNT)


async def _checkpoint(rt: Runtime) -> Checkpoint:
    """A real checkpoint of a real computer, so fork has a disk and snapshot files."""
    computer = await rt.computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    return await rt.checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)


async def _vm_ip(db: aiosqlite.Connection, computer_id: str) -> str:
    computer = await get_computer(db, computer_id)
    assert computer is not None
    return computer.vm_ip


async def test_create_without_exec_works_as_before(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Create without exec field returns no exec results."""
    await _account(db)
    host = FakeHost()
    app = make_app(make_runtime(db, config=runtime_config, host=host))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"].startswith("comp-")
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
    assert host.guest.commands == []


async def test_create_with_exec_returns_results(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Create with exec runs the command and returns results."""
    await _account(db)
    host = FakeHost()
    host.guest.script["echo hello world"] = ExecResult(
        exit_code=0, stdout="hello world\n", stderr=""
    )
    app = make_app(make_runtime(db, config=runtime_config, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/computers",
            json={"exec": "echo hello world"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"].startswith("comp-")
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "hello world\n"
    assert data["exec_stderr"] == ""
    assert host.guest.commands == [
        (await _vm_ip(db, data["computer_id"]), "echo hello world"),
    ]


async def test_create_with_exec_nonzero_exit(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Create with exec that fails still returns the result (not an error)."""
    await _account(db)
    host = FakeHost()
    host.guest.script["bad-command"] = ExecResult(
        exit_code=1, stdout="", stderr="command not found\n"
    )
    app = make_app(make_runtime(db, config=runtime_config, host=host))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/computers", json={"exec": "bad-command"}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["exec_exit_code"] == 1
    assert data["exec_stderr"] == "command not found\n"
    assert host.guest.commands == [(await _vm_ip(db, data["computer_id"]), "bad-command")]


async def test_fork_with_exec_returns_results(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Fork with exec runs the command after restore and returns results."""
    await _account(db)
    host = FakeHost()
    host.guest.script["echo forked output"] = ExecResult(
        exit_code=0, stdout="forked output\n", stderr=""
    )
    rt = make_runtime(db, config=runtime_config, host=host)
    ckpt = await _checkpoint(rt)
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/checkpoints/{ckpt.id}/fork",
            json={"exec": "echo forked output"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"].startswith("comp-")
    assert data["checkpoint_id"] == ckpt.id
    assert data["exec_exit_code"] == 0
    assert data["exec_stdout"] == "forked output\n"
    assert data["exec_stderr"] == ""
    forked_ip = await _vm_ip(db, data["computer_id"])
    assert host.guest.commands[-1] == (forked_ip, "echo forked output")


async def test_fork_without_exec_works_as_before(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Fork without exec returns no exec results."""
    await _account(db)
    host = FakeHost()
    rt = make_runtime(db, config=runtime_config, host=host)
    ckpt = await _checkpoint(rt)
    commands_before = list(host.guest.commands)
    app = make_app(rt)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/checkpoints/{ckpt.id}/fork", json={}, headers=AUTH)

    assert resp.status_code == 200
    data = resp.json()
    assert data["computer_id"].startswith("comp-")
    assert data["exec_exit_code"] is None
    assert data["exec_stdout"] is None
    assert data["exec_stderr"] is None
    assert host.guest.commands == commands_before
