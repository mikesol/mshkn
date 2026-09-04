"""Tests for exclusive restore with conflict modes (issue #30)."""

from pathlib import Path

import aiosqlite

from mshkn.db import (
    delete_deferred_by_label,
    get_active_computer_for_label,
    insert_account,
    insert_checkpoint,
    insert_computer,
    insert_deferred,
    list_deferred_by_label,
    run_migrations,
)
from mshkn.models import Account, Checkpoint, Computer


def _account() -> Account:
    return Account(
        id="acct-1",
        api_key="key-abc",
        vm_limit=10,
        created_at="2026-03-08T00:00:00",
    )


def _checkpoint(
    checkpoint_id: str = "ckpt-1",
    label: str | None = "my-agent",
    computer_id: str | None = "comp-1",
) -> Checkpoint:
    return Checkpoint(
        id=checkpoint_id,
        account_id="acct-1",
        parent_id=None,
        computer_id=computer_id,
        thin_volume_id=42,
        manifest_hash="abc",
        manifest_json='{"uses":[]}',
        r2_prefix="acct-1/ckpt-1",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=label,
        pinned=False,
        created_at="2026-03-08T00:00:00",
    )


def _computer(
    computer_id: str = "comp-1",
    status: str = "running",
    source_checkpoint_id: str | None = "ckpt-1",
) -> Computer:
    return Computer(
        id=computer_id,
        account_id="acct-1",
        thin_volume_id=1,
        tap_device="tap1",
        vm_ip="172.16.1.2",
        socket_path="/tmp/fc.socket",
        firecracker_pid=999,
        manifest_hash="abc",
        manifest_json='{"uses":[]}',
        status=status,
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
        source_checkpoint_id=source_checkpoint_id,
    )


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(db, _account())
    return db


async def test_get_active_computer_for_label_returns_running(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = _checkpoint()
        await insert_checkpoint(db, ckpt)
        comp = _computer(source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is not None
        assert result.id == "comp-1"
    finally:
        await db.close()


async def test_get_active_computer_for_label_ignores_destroyed(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = _checkpoint()
        await insert_checkpoint(db, ckpt)
        comp = _computer(status="destroyed", source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is None
    finally:
        await db.close()


async def test_get_active_computer_for_label_no_match(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = _checkpoint(label="other-label")
        await insert_checkpoint(db, ckpt)
        comp = _computer(source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is None
    finally:
        await db.close()


async def test_get_active_computer_for_label_no_label(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = _checkpoint(label=None)
        await insert_checkpoint(db, ckpt)
        comp = _computer(source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is None
    finally:
        await db.close()


async def test_deferred_queue_insert_and_list(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_deferred(
            db, "def-1", "my-agent", "acct-1",
            '{"checkpoint_id":"ckpt-1"}', "2026-03-08T00:00:00",
        )
        await insert_deferred(
            db, "def-2", "my-agent", "acct-1",
            '{"checkpoint_id":"ckpt-1"}', "2026-03-08T00:01:00",
        )

        items = await list_deferred_by_label(db, "my-agent")
        assert len(items) == 2
        assert items[0]["id"] == "def-1"
        assert items[1]["id"] == "def-2"
    finally:
        await db.close()


async def test_deferred_queue_delete_by_label(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_deferred(
            db, "def-1", "my-agent", "acct-1",
            '{"checkpoint_id":"ckpt-1"}', "2026-03-08T00:00:00",
        )
        await insert_deferred(
            db, "def-2", "other-agent", "acct-1",
            '{"checkpoint_id":"ckpt-2"}', "2026-03-08T00:01:00",
        )

        await delete_deferred_by_label(db, "my-agent")

        items_agent = await list_deferred_by_label(db, "my-agent")
        assert len(items_agent) == 0

        items_other = await list_deferred_by_label(db, "other-agent")
        assert len(items_other) == 1
    finally:
        await db.close()


async def test_deferred_queue_empty_label(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        items = await list_deferred_by_label(db, "nonexistent")
        assert len(items) == 0
    finally:
        await db.close()


async def test_migration_creates_deferred_queue_table(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deferred_queue'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "deferred_queue"
    finally:
        await db.close()
