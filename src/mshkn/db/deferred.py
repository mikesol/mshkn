"""deferred_queue table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import DeferredRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "label",
    "account_id",
    "request_payload",
    "created_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM deferred_queue"


def _row_to_deferred(row: Sequence[object]) -> DeferredRequest:
    d = dict(zip(COLUMNS, row, strict=True))
    return DeferredRequest(
        id=str(d["id"]),
        label=str(d["label"]),
        account_id=str(d["account_id"]),
        request_payload=str(d["request_payload"]),
        created_at=str(d["created_at"]),
    )


async def insert_deferred(
    db: aiosqlite.Connection,
    deferred_id: str,
    label: str,
    account_id: str,
    payload_json: str,
    created_at: str,
) -> None:
    """Insert a deferred request into the queue."""
    await db.execute(
        "INSERT INTO deferred_queue (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (deferred_id, label, account_id, payload_json, created_at),
    )
    await db.commit()


async def list_deferred_by_label(db: aiosqlite.Connection, label: str) -> list[DeferredRequest]:
    """Return all deferred requests for a label, ordered by created_at ASC."""
    cursor = await db.execute(
        _SELECT + " WHERE label = ? ORDER BY created_at ASC",
        (label,),
    )
    return [_row_to_deferred(r) for r in await cursor.fetchall()]


async def delete_deferred_by_label(db: aiosqlite.Connection, label: str) -> None:
    """Delete all deferred requests for a label."""
    await db.execute("DELETE FROM deferred_queue WHERE label = ?", (label,))
    await db.commit()
