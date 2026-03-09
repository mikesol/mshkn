from pathlib import Path

import aiosqlite

from mshkn.db import (
    get_latest_checkpoint_for_computer,
    insert_account,
    insert_checkpoint,
    insert_computer,
    run_migrations,
)
from mshkn.models import Account, Checkpoint, Computer


async def _setup_db(tmp_path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(tmp_path / "test.db")
    await run_migrations(db, Path("migrations"))
    await insert_account(
        db,
        Account(id="acct-1", api_key="key-abc", vm_limit=10, created_at="2026-03-08T00:00:00"),
    )
    return db


def _make_computer(
    computer_id: str = "comp-1",
    source_checkpoint_id: str | None = None,
) -> Computer:
    return Computer(
        id=computer_id,
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
        source_checkpoint_id=source_checkpoint_id,
    )


def _make_checkpoint(
    checkpoint_id: str,
    computer_id: str = "comp-1",
    parent_id: str | None = None,
    created_at: str = "2026-03-08T00:00:00",
) -> Checkpoint:
    return Checkpoint(
        id=checkpoint_id,
        account_id="acct-1",
        parent_id=parent_id,
        computer_id=computer_id,
        thin_volume_id=42,
        manifest_hash="abc",
        manifest_json="{}",
        r2_prefix=f"acct-1/{checkpoint_id}",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at=created_at,
    )


async def test_get_latest_checkpoint_for_computer_returns_most_recent(tmp_path: Path) -> None:
    db = await _setup_db(tmp_path)
    try:
        await insert_computer(db, _make_computer())
        await insert_checkpoint(db, _make_checkpoint("ckpt-1", created_at="2026-03-08T01:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-2", created_at="2026-03-08T02:00:00"))
        await insert_checkpoint(db, _make_checkpoint("ckpt-3", created_at="2026-03-08T03:00:00"))

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
        await insert_computer(db, _make_computer())
        # First checkpoint has no parent
        await insert_checkpoint(db, _make_checkpoint("ckpt-1", created_at="2026-03-08T01:00:00"))

        # Simulate what checkpoint_computer() does: look up latest, use as parent
        latest = await get_latest_checkpoint_for_computer(db, "comp-1")
        assert latest is not None
        parent_id = latest.id

        ckpt2 = _make_checkpoint("ckpt-2", parent_id=parent_id, created_at="2026-03-08T02:00:00")
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
        await insert_computer(db, _make_computer("comp-orig"))
        await insert_checkpoint(
            db,
            _make_checkpoint("ckpt-orig", computer_id="comp-orig", created_at="2026-03-08T01:00:00"),
        )

        # Forked computer with source_checkpoint_id set
        forked = _make_computer("comp-fork", source_checkpoint_id="ckpt-orig")
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
        ckpt = _make_checkpoint("ckpt-fork", computer_id="comp-fork", parent_id=parent_id)
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
        await insert_computer(db, _make_computer())

        latest = await get_latest_checkpoint_for_computer(db, "comp-1")
        assert latest is None

        from mshkn.db import get_computer

        comp = await get_computer(db, "comp-1")
        assert comp is not None
        assert comp.source_checkpoint_id is None

        # parent_id stays None
        ckpt = _make_checkpoint("ckpt-1")
        await insert_checkpoint(db, ckpt)

        from mshkn.db import get_checkpoint

        result = await get_checkpoint(db, "ckpt-1")
        assert result is not None
        assert result.parent_id is None
    finally:
        await db.close()
