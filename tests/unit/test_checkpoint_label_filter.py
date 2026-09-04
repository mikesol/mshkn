from pathlib import Path

import aiosqlite

from mshkn.db import (
    insert_account,
    insert_checkpoint,
    list_checkpoints_by_account,
    run_migrations,
)
from mshkn.models import Account, Checkpoint


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
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
    return db


def _make_checkpoint(
    id: str, label: str | None, created_at: str
) -> Checkpoint:
    return Checkpoint(
        id=id,
        account_id="acct-1",
        parent_id=None,
        computer_id="comp-1",
        thin_volume_id=1,
        manifest_hash="abc",
        manifest_json='{"uses":[]}',
        r2_prefix=f"acct-1/{id}",
        disk_delta_size_bytes=1024,
        memory_size_bytes=512000,
        label=label,
        pinned=False,
        created_at=created_at,
    )


async def test_list_checkpoints_no_label_returns_all(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_checkpoint(db, _make_checkpoint("ckpt-1", "chat-123", "2026-03-08T01:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-2", "chat-456", "2026-03-08T02:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-3", None, "2026-03-08T03:00:00"))

        results = await list_checkpoints_by_account(db, "acct-1")
        assert len(results) == 3
    finally:
        await db.close()


async def test_list_checkpoints_with_label_filters(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_checkpoint(db, _make_checkpoint("ckpt-1", "chat-123", "2026-03-08T01:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-2", "chat-456", "2026-03-08T02:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-3", "chat-123", "2026-03-08T03:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-4", None, "2026-03-08T04:00:00"))

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
        await insert_checkpoint(db, _make_checkpoint("ckpt-1", "chat-123", "2026-03-08T01:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-2", "chat-123", "2026-03-08T03:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-3", "chat-123", "2026-03-08T02:00:00"))

        results = await list_checkpoints_by_account(db, "acct-1", label="chat-123")
        assert len(results) == 3
        # Should be ordered newest first
        assert results[0].id == "ckpt-2"
        assert results[1].id == "ckpt-3"
        assert results[2].id == "ckpt-1"
    finally:
        await db.close()
