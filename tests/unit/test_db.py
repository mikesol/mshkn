from pathlib import Path

import aiosqlite

from mshkn.db import (
    count_active_computers,
    count_active_computers_by_account,
    get_account_by_key,
    get_checkpoint,
    get_computer,
    insert_account,
    insert_checkpoint,
    insert_computer,
    list_accounts,
    run_migrations,
    update_computer_status,
)
from mshkn.models import Account, Checkpoint, Computer, ComputerStatus


def _computer(
    computer_id: str,
    *,
    account_id: str = "acct-1",
    status: ComputerStatus = ComputerStatus.RUNNING,
) -> Computer:
    return Computer(
        id=computer_id,
        account_id=account_id,
        thin_volume_id=1,
        tap_device="tap1",
        vm_ip="172.16.1.2",
        socket_path=f"/tmp/fc-{computer_id}.socket",
        firecracker_pid=999,
        status=status,
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
    )


def _checkpoint(
    checkpoint_id: str,
    *,
    account_id: str = "acct-1",
) -> Checkpoint:
    return Checkpoint(
        id=checkpoint_id,
        account_id=account_id,
        parent_id=None,
        computer_id=None,
        thin_volume_id=None,
        r2_prefix=f"{account_id}/{checkpoint_id}",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )


async def test_migrations_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path("migrations")
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, migrations_dir)
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in await cursor.fetchall()]
    assert "accounts" in tables
    assert "computers" in tables
    assert "checkpoints" in tables
    assert "deferred_queue" in tables


async def test_migrations_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path("migrations")
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, migrations_dir)
        await run_migrations(db, migrations_dir)  # second run should be a no-op


async def test_account_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(
            db,
            Account(
                id="acct-1",
                api_key="key-abc",
                vm_limit=10,
                created_at="2026-03-08T00:00:00",
            ),
        )
        result = await get_account_by_key(db, "key-abc")
    assert result is not None
    assert result.id == "acct-1"


async def test_computer_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(
            db,
            Account(
                id="acct-1",
                api_key="key-abc",
                vm_limit=10,
                created_at="2026-03-08T00:00:00",
            ),
        )
        comp = Computer(
            id="comp-1",
            account_id="acct-1",
            thin_volume_id=1,
            tap_device="tap1",
            vm_ip="172.16.1.2",
            socket_path="/tmp/fc-comp-1.socket",
            firecracker_pid=999,
            status=ComputerStatus.RUNNING,
            created_at="2026-03-08T00:00:00",
            last_exec_at=None,
        )
        await insert_computer(db, comp)
        result = await get_computer(db, "comp-1")
    assert result is not None
    assert result.vm_ip == "172.16.1.2"
    assert result.status == ComputerStatus.RUNNING


async def test_update_computer_status(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(
            db,
            Account(
                id="acct-1",
                api_key="key-abc",
                vm_limit=10,
                created_at="2026-03-08T00:00:00",
            ),
        )
        comp = Computer(
            id="comp-1",
            account_id="acct-1",
            thin_volume_id=1,
            tap_device="tap1",
            vm_ip="172.16.1.2",
            socket_path="/tmp/fc-comp-1.socket",
            firecracker_pid=999,
            status=ComputerStatus.RUNNING,
            created_at="2026-03-08T00:00:00",
            last_exec_at=None,
        )
        await insert_computer(db, comp)
        await update_computer_status(db, "comp-1", ComputerStatus.DESTROYED)
        result = await get_computer(db, "comp-1")
    assert result is not None
    assert result.status == ComputerStatus.DESTROYED


async def test_count_active_computers(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(
            db,
            Account(
                id="acct-1",
                api_key="key-abc",
                vm_limit=10,
                created_at="2026-03-08T00:00:00",
            ),
        )
        # No computers yet
        assert await count_active_computers_by_account(db, "acct-1") == 0

        # Add a running computer
        await insert_computer(
            db,
            Computer(
                id="comp-1",
                account_id="acct-1",
                thin_volume_id=1,
                tap_device="tap1",
                vm_ip="172.16.1.2",
                socket_path="/tmp/fc.socket",
                firecracker_pid=999,
                status=ComputerStatus.RUNNING,
                created_at="2026-03-08T00:00:00",
                last_exec_at=None,
            ),
        )
        assert await count_active_computers_by_account(db, "acct-1") == 1

        # Add a destroyed computer — should not count
        await insert_computer(
            db,
            Computer(
                id="comp-2",
                account_id="acct-1",
                thin_volume_id=2,
                tap_device="tap2",
                vm_ip="172.16.1.3",
                socket_path="/tmp/fc2.socket",
                firecracker_pid=1000,
                status=ComputerStatus.DESTROYED,
                created_at="2026-03-08T00:00:00",
                last_exec_at=None,
            ),
        )
        assert await count_active_computers_by_account(db, "acct-1") == 1

        # Different account should be 0
        assert await count_active_computers_by_account(db, "acct-other") == 0


async def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, Path("migrations"))
        await insert_account(
            db,
            Account(
                id="acct-1",
                api_key="key-abc",
                vm_limit=10,
                created_at="2026-03-08T00:00:00",
            ),
        )
        ckpt = Checkpoint(
            id="ckpt-1",
            account_id="acct-1",
            parent_id=None,
            computer_id="comp-1",
            thin_volume_id=42,
            r2_prefix="acct-1/ckpt-1",
            disk_delta_size_bytes=1024,
            memory_size_bytes=512000,
            label="initial",
            pinned=False,
            created_at="2026-03-08T00:00:00",
        )
        await insert_checkpoint(db, ckpt)
        result = await get_checkpoint(db, "ckpt-1")
    assert result is not None
    assert result.r2_prefix == "acct-1/ckpt-1"
    assert result.parent_id is None
    assert result.thin_volume_id == 42


async def test_insert_writes_manifest_placeholders(db: aiosqlite.Connection) -> None:
    await insert_account(db, Account(id="acct-1", api_key="k", vm_limit=1, created_at="t"))
    await insert_computer(db, _computer("comp-1"))
    cur = await db.execute("SELECT manifest_hash, manifest_json FROM computers WHERE id = 'comp-1'")
    assert await cur.fetchone() == ("", "{}")
    await insert_checkpoint(db, _checkpoint("ckpt-1"))
    cur = await db.execute(
        "SELECT manifest_hash, manifest_json FROM checkpoints WHERE id = 'ckpt-1'"
    )
    assert await cur.fetchone() == ("", "{}")


async def test_count_active_computers_spans_accounts(db: aiosqlite.Connection) -> None:
    await insert_account(db, Account(id="acct-1", api_key="k1", vm_limit=5, created_at="t"))
    await insert_account(db, Account(id="acct-2", api_key="k2", vm_limit=5, created_at="t"))
    await insert_computer(db, _computer("c1", account_id="acct-1"))
    await insert_computer(db, _computer("c2", account_id="acct-2"))
    await insert_computer(db, _computer("c3", account_id="acct-2", status=ComputerStatus.DESTROYED))
    assert await count_active_computers(db) == 2
    assert [a.id for a in await list_accounts(db)] == ["acct-1", "acct-2"]
