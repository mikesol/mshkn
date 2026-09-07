from pathlib import Path

import aiosqlite

from mshkn.db import (
    get_latest_checkpoint_for_computer,
    insert_account,
    insert_checkpoint,
    insert_computer,
    run_migrations,
)
from tests.support import account_row, checkpoint_row, computer_row


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await insert_account(db, account_row(api_key="key-abc"))
    return db


async def test_get_latest_checkpoint_for_computer_returns_most_recent(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_computer(db, computer_row())
        await insert_checkpoint(db, checkpoint_row("ckpt-1", created_at="2026-03-08T01:00:00"))
        await insert_checkpoint(db, checkpoint_row("ckpt-2", created_at="2026-03-08T02:00:00"))
        await insert_checkpoint(db, checkpoint_row("ckpt-3", created_at="2026-03-08T03:00:00"))

        latest = await get_latest_checkpoint_for_computer(db, "comp-1")
        assert latest is not None
        assert latest.id == "ckpt-3"
    finally:
        await db.close()


async def test_get_latest_checkpoint_for_computer_returns_none_when_empty(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        result = await get_latest_checkpoint_for_computer(db, "comp-nonexistent")
        assert result is None
    finally:
        await db.close()


async def test_checkpoint_parent_id_from_prior_checkpoint(tmp_path: Path) -> None:
    """Second checkpoint on same computer should have parent_id = first checkpoint's ID."""
    db = await _setup_db(tmp_path)
    try:
        await insert_computer(db, computer_row())
        # First checkpoint has no parent
        await insert_checkpoint(db, checkpoint_row("ckpt-1", created_at="2026-03-08T01:00:00"))

        # Simulate what checkpoint_computer() does: look up latest, use as parent
        latest = await get_latest_checkpoint_for_computer(db, "comp-1")
        assert latest is not None
        parent_id = latest.id

        ckpt2 = checkpoint_row("ckpt-2", parent_id=parent_id, created_at="2026-03-08T02:00:00")
        await insert_checkpoint(db, ckpt2)

        from mshkn.db import get_checkpoint

        result = await get_checkpoint(db, "ckpt-2")
        assert result is not None
        assert result.parent_id == "ckpt-1"
    finally:
        await db.close()


async def test_first_checkpoint_of_forked_computer_gets_source_parent(tmp_path: Path) -> None:
    """First checkpoint of a forked computer should have parent_id = source checkpoint ID."""
    db = await _setup_db(tmp_path)
    try:
        # Original computer and its checkpoint
        await insert_computer(db, computer_row(id="comp-orig"))
        await insert_checkpoint(
            db,
            checkpoint_row("ckpt-orig", computer_id="comp-orig", created_at="2026-03-08T01:00:00"),
        )

        # Forked computer with source_checkpoint_id set
        forked = computer_row(id="comp-fork", source_checkpoint_id="ckpt-orig")
        await insert_computer(db, forked)

        # Simulate checkpoint_computer() logic for the forked computer
        latest = await get_latest_checkpoint_for_computer(db, "comp-fork")
        assert latest is None  # no prior checkpoints on this computer

        # Falls back to source_checkpoint_id
        from mshkn.db import get_computer

        comp = await get_computer(db, "comp-fork")
        assert comp is not None
        assert comp.source_checkpoint_id == "ckpt-orig"

        parent_id = comp.source_checkpoint_id
        ckpt = checkpoint_row("ckpt-fork", computer_id="comp-fork", parent_id=parent_id)
        await insert_checkpoint(db, ckpt)

        from mshkn.db import get_checkpoint

        result = await get_checkpoint(db, "ckpt-fork")
        assert result is not None
        assert result.parent_id == "ckpt-orig"
    finally:
        await db.close()


async def test_computer_without_source_gets_no_parent(tmp_path: Path) -> None:
    """First checkpoint of a non-forked computer should have parent_id = None."""
    db = await _setup_db(tmp_path)
    try:
        await insert_computer(db, computer_row())

        latest = await get_latest_checkpoint_for_computer(db, "comp-1")
        assert latest is None

        from mshkn.db import get_computer

        comp = await get_computer(db, "comp-1")
        assert comp is not None
        assert comp.source_checkpoint_id is None

        # parent_id stays None
        ckpt = checkpoint_row("ckpt-1")
        await insert_checkpoint(db, ckpt)

        from mshkn.db import get_checkpoint

        result = await get_checkpoint(db, "ckpt-1")
        assert result is not None
        assert result.parent_id is None
    finally:
        await db.close()
