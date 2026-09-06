"""checkpoints table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import Checkpoint

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "parent_id",
    "computer_id",
    "thin_volume_id",
    "r2_prefix",
    "disk_delta_size_bytes",
    "memory_size_bytes",
    "label",
    "pinned",
    "created_at",
    "recipe_id",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM checkpoints"


def _row_to_checkpoint(row: Sequence[object]) -> Checkpoint:
    d = dict(zip(COLUMNS, row, strict=True))
    return Checkpoint(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        parent_id=None if d["parent_id"] is None else str(d["parent_id"]),
        computer_id=None if d["computer_id"] is None else str(d["computer_id"]),
        thin_volume_id=None if d["thin_volume_id"] is None else int(d["thin_volume_id"]),  # type: ignore[call-overload]
        r2_prefix=str(d["r2_prefix"]),
        disk_delta_size_bytes=(
            None if d["disk_delta_size_bytes"] is None else int(d["disk_delta_size_bytes"])  # type: ignore[call-overload]
        ),
        memory_size_bytes=(
            None if d["memory_size_bytes"] is None else int(d["memory_size_bytes"])  # type: ignore[call-overload]
        ),
        label=None if d["label"] is None else str(d["label"]),
        pinned=bool(d["pinned"]),
        created_at=str(d["created_at"]),
        recipe_id=None if d["recipe_id"] is None else str(d["recipe_id"]),
    )


async def insert_checkpoint(db: aiosqlite.Connection, checkpoint: Checkpoint) -> None:
    await db.execute(
        "INSERT INTO checkpoints (" + ", ".join(COLUMNS) + ", manifest_hash, manifest_json) "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ", '', '{}')",
        (
            checkpoint.id,
            checkpoint.account_id,
            checkpoint.parent_id,
            checkpoint.computer_id,
            checkpoint.thin_volume_id,
            checkpoint.r2_prefix,
            checkpoint.disk_delta_size_bytes,
            checkpoint.memory_size_bytes,
            checkpoint.label,
            int(checkpoint.pinned),
            checkpoint.created_at,
            checkpoint.recipe_id,
        ),
    )
    await db.commit()


async def get_checkpoint(db: aiosqlite.Connection, checkpoint_id: str) -> Checkpoint | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (checkpoint_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_checkpoint(row)


async def list_checkpoints_by_account(
    db: aiosqlite.Connection, account_id: str, label: str | None = None
) -> list[Checkpoint]:
    query = _SELECT + " WHERE account_id = ?"
    params: list[str] = [account_id]
    if label is not None:
        query += " AND label = ?"
        params.append(label)
    query += " ORDER BY created_at DESC"
    cursor = await db.execute(query, params)
    return [_row_to_checkpoint(r) for r in await cursor.fetchall()]


async def get_latest_checkpoint_for_computer(
    db: aiosqlite.Connection, computer_id: str
) -> Checkpoint | None:
    """Return the most recent checkpoint for a given computer_id, or None."""
    cursor = await db.execute(
        _SELECT + " WHERE computer_id = ? ORDER BY created_at DESC LIMIT 1",
        (computer_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_checkpoint(row)


async def get_max_checkpoint_volume_id(db: aiosqlite.Connection) -> int | None:
    """Return the highest thin_volume_id across all checkpoints, or None."""
    cursor = await db.execute(
        "SELECT MAX(thin_volume_id) FROM checkpoints WHERE thin_volume_id IS NOT NULL"
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else None


async def delete_checkpoint(db: aiosqlite.Connection, checkpoint_id: str) -> None:
    await db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
    await db.commit()


async def list_prunable_checkpoints(
    db: aiosqlite.Connection, account_id: str, keep_count: int
) -> list[Checkpoint]:
    """Return unpinned checkpoints beyond the keep_count newest, oldest first.

    Pinned checkpoints are never returned. The keep_count newest unpinned
    checkpoints are preserved; everything older is returned for pruning.
    """
    cursor = await db.execute(
        _SELECT + " WHERE account_id = ? AND pinned = 0 ORDER BY created_at DESC",
        (account_id,),
    )
    rows = list(await cursor.fetchall())
    # Skip the first keep_count (newest), return the rest
    excess = rows[keep_count:]
    return [_row_to_checkpoint(r) for r in excess]


async def list_account_ids_with_checkpoints(db: aiosqlite.Connection) -> list[str]:
    """Return distinct account IDs that have at least one checkpoint."""
    cursor = await db.execute("SELECT DISTINCT account_id FROM checkpoints")
    rows = await cursor.fetchall()
    return [r[0] for r in rows]
