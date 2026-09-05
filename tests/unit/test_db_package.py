from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import connect, run_migrations
from mshkn.db.computers import COLUMNS as COMPUTER_COLUMNS
from mshkn.db.computers import _row_to_computer
from mshkn.models import ComputerStatus

if TYPE_CHECKING:
    import aiosqlite


async def _pragma(db: aiosqlite.Connection, name: str) -> object:
    cursor = await db.execute(f"PRAGMA {name}")
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def test_connect_sets_pragmas(tmp_path: Path) -> None:
    db = await connect(tmp_path / "t.db")
    try:
        assert await _pragma(db, "journal_mode") == "wal"
        assert await _pragma(db, "synchronous") == 1  # NORMAL
        assert await _pragma(db, "busy_timeout") == 5000
        assert await _pragma(db, "foreign_keys") == 0  # deliberately off, see plan Task 5
    finally:
        await db.close()


async def test_migrations_create_indexes(tmp_path: Path) -> None:
    db = await connect(tmp_path / "t.db")
    try:
        await run_migrations(db, Path("migrations"))
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        names = {row[0] for row in await cursor.fetchall()}
    finally:
        await db.close()
    assert {
        "idx_computers_account_status",
        "idx_checkpoints_account_created",
        "idx_checkpoints_computer_created",
        "idx_checkpoints_label",
        "idx_deferred_queue_label_created",
    } <= names


async def test_migrations_record_each_file_once(tmp_path: Path) -> None:
    db = await connect(tmp_path / "t.db")
    try:
        await run_migrations(db, Path("migrations"))
        await run_migrations(db, Path("migrations"))
        cursor = await db.execute("SELECT filename FROM _migrations ORDER BY filename")
        applied = [row[0] for row in await cursor.fetchall()]
    finally:
        await db.close()
    expected = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert applied == expected


def test_row_mapper_uses_column_order() -> None:
    row: list[object] = [None] * len(COMPUTER_COLUMNS)
    row[COMPUTER_COLUMNS.index("id")] = "comp-1"
    row[COMPUTER_COLUMNS.index("account_id")] = "acct-1"
    row[COMPUTER_COLUMNS.index("thin_volume_id")] = 7
    row[COMPUTER_COLUMNS.index("tap_device")] = "tap1"
    row[COMPUTER_COLUMNS.index("vm_ip")] = "172.16.1.2"
    row[COMPUTER_COLUMNS.index("socket_path")] = "/tmp/s"
    row[COMPUTER_COLUMNS.index("manifest_hash")] = "none"
    row[COMPUTER_COLUMNS.index("manifest_json")] = "{}"
    row[COMPUTER_COLUMNS.index("status")] = "running"
    row[COMPUTER_COLUMNS.index("created_at")] = "t"
    computer = _row_to_computer(row)
    assert computer.id == "comp-1"
    assert computer.thin_volume_id == 7
    assert computer.status is ComputerStatus.RUNNING
    assert computer.recipe_id is None
