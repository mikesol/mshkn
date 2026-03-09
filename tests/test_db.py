from pathlib import Path

import aiosqlite

from mshkn.db import (
    count_active_computers_by_account,
    get_account_by_key,
    get_checkpoint,
    get_computer,
    insert_account,
    insert_checkpoint,
    insert_computer,
    run_migrations,
    update_computer_status,
)
from mshkn.models import Account, Checkpoint, Computer


async def test_migrations_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    migrations_dir = Path("migrations")
    async with aiosqlite.connect(db_path) as db:
        await run_migrations(db, migrations_dir)
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
    assert "accounts" in tables
    assert "computers" in tables
    assert "checkpoints" in tables
    assert "capability_cache" in tables


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
            manifest_hash="abc",
            status="running",
            created_at="2026-03-08T00:00:00",
            last_exec_at=None,
        )
        await insert_computer(db, comp)
        result = await get_computer(db, "comp-1")
    assert result is not None
    assert result.vm_ip == "172.16.1.2"
    assert result.status == "running"


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
            manifest_hash="abc",
            status="running",
            created_at="2026-03-08T00:00:00",
            last_exec_at=None,
        )
        await insert_computer(db, comp)
        await update_computer_status(db, "comp-1", "destroyed")
        result = await get_computer(db, "comp-1")
    assert result is not None
    assert result.status == "destroyed"


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
                manifest_hash="abc",
                status="running",
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
                manifest_hash="abc",
                status="destroyed",
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
            manifest_hash="abc",
            manifest_json='{"uses":["python-3.12"]}',
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
    assert result.manifest_hash == "abc"
    assert result.parent_id is None
    assert result.thin_volume_id == 42
