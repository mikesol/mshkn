from pathlib import Path

import aiosqlite

from mshkn.db import (
    insert_account,
    insert_checkpoint,
    list_checkpoints_by_account,
    run_migrations,
)
from tests.support import account_row, checkpoint_row


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await insert_account(db, account_row(api_key="key-abc"))
    return db


async def test_list_checkpoints_no_label_returns_all(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_checkpoint(
            db, checkpoint_row("ckpt-1", label="chat-123", created_at="2026-03-08T01:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-2", label="chat-456", created_at="2026-03-08T02:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-3", label=None, created_at="2026-03-08T03:00:00")
        )

        results = await list_checkpoints_by_account(db, "acct-1")
        assert len(results) == 3
    finally:
        await db.close()


async def test_list_checkpoints_with_label_filters(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_checkpoint(
            db, checkpoint_row("ckpt-1", label="chat-123", created_at="2026-03-08T01:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-2", label="chat-456", created_at="2026-03-08T02:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-3", label="chat-123", created_at="2026-03-08T03:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-4", label=None, created_at="2026-03-08T04:00:00")
        )

        results = await list_checkpoints_by_account(db, "acct-1", label="chat-123")
        assert len(results) == 2
        assert all(r.label == "chat-123" for r in results)

        results = await list_checkpoints_by_account(db, "acct-1", label="chat-456")
        assert len(results) == 1
        assert results[0].id == "ckpt-2"

        results = await list_checkpoints_by_account(db, "acct-1", label="nonexistent")
        assert len(results) == 0
    finally:
        await db.close()


async def test_list_checkpoints_ordered_by_created_at_desc(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_checkpoint(
            db, checkpoint_row("ckpt-1", label="chat-123", created_at="2026-03-08T01:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-2", label="chat-123", created_at="2026-03-08T03:00:00")
        )
        await insert_checkpoint(
            db, checkpoint_row("ckpt-3", label="chat-123", created_at="2026-03-08T02:00:00")
        )

        results = await list_checkpoints_by_account(db, "acct-1", label="chat-123")
        assert len(results) == 3
        # Should be ordered newest first
        assert results[0].id == "ckpt-2"
        assert results[1].id == "ckpt-3"
        assert results[2].id == "ckpt-1"
    finally:
        await db.close()
