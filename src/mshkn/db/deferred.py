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


async def claim_deferred_by_label(db: aiosqlite.Connection, label: str) -> list[DeferredRequest]:
    """Atomically take every queued request for a label, oldest first.

    One statement, so two drains racing on the same label (a destroy and an
    idle reap, say) cannot both receive the batch: SQLite serialises writers
    and the second DELETE finds nothing.
    """
    cursor = await db.execute(
        "DELETE FROM deferred_queue WHERE label = ? RETURNING " + ", ".join(COLUMNS),
        (label,),
    )
    rows = await cursor.fetchall()
    await db.commit()
    items = [_row_to_deferred(r) for r in rows]
    items.sort(key=lambda d: (d.created_at, d.id))
    return items
