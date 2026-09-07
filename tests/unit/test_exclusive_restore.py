"""Tests for exclusive restore with conflict modes (issue #30)."""

from pathlib import Path

import aiosqlite

from mshkn.db import (
    claim_deferred_by_label,
    get_active_computer_for_label,
    insert_account,
    insert_checkpoint,
    insert_computer,
    insert_deferred,
    run_migrations,
)
from mshkn.models import ComputerStatus
from tests.support import account_row, checkpoint_row, computer_row


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(db_path)
    await run_migrations(db, Path("migrations"))
    await insert_account(db, account_row(api_key="key-abc"))
    return db


async def test_get_active_computer_for_label_returns_running(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = checkpoint_row(label="my-agent", thin_volume_id=42)
        await insert_checkpoint(db, ckpt)
        comp = computer_row(source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is not None
        assert result.id == "comp-1"
    finally:
        await db.close()


async def test_get_active_computer_for_label_ignores_destroyed(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = checkpoint_row(label="my-agent", thin_volume_id=42)
        await insert_checkpoint(db, ckpt)
        comp = computer_row(status=ComputerStatus.DESTROYED, source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is None
    finally:
        await db.close()


async def test_get_active_computer_for_label_no_match(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = checkpoint_row(label="other-label", thin_volume_id=42)
        await insert_checkpoint(db, ckpt)
        comp = computer_row(source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is None
    finally:
        await db.close()


async def test_get_active_computer_for_label_no_label(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        ckpt = checkpoint_row(label=None, thin_volume_id=42)
        await insert_checkpoint(db, ckpt)
        comp = computer_row(source_checkpoint_id="ckpt-1")
        await insert_computer(db, comp)

        result = await get_active_computer_for_label(db, "acct-1", "my-agent")
        assert result is None
    finally:
        await db.close()


async def test_deferred_queue_insert_and_list(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_deferred(
            db,
            "def-1",
            "my-agent",
            "acct-1",
            '{"checkpoint_id":"ckpt-1"}',
            "2026-03-08T00:00:00",
        )
        await insert_deferred(
            db,
            "def-2",
            "my-agent",
            "acct-1",
            '{"checkpoint_id":"ckpt-1"}',
            "2026-03-08T00:01:00",
        )

        items = await claim_deferred_by_label(db, "my-agent")
        assert len(items) == 2
        assert items[0].id == "def-1"
        assert items[1].id == "def-2"
    finally:
        await db.close()


async def test_deferred_queue_empty_label(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        items = await claim_deferred_by_label(db, "nonexistent")
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
