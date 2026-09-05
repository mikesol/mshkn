"""accounts table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import Account

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "api_key",
    "vm_limit",
    "created_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM accounts"


def _row_to_account(row: Sequence[object]) -> Account:
    d = dict(zip(COLUMNS, row, strict=True))
    return Account(
        id=str(d["id"]),
        api_key=str(d["api_key"]),
        vm_limit=int(d["vm_limit"]),  # type: ignore[call-overload]
        created_at=str(d["created_at"]),
    )


async def insert_account(db: aiosqlite.Connection, account: Account) -> None:
    await db.execute(
        "INSERT INTO accounts (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (
            account.id,
            account.api_key,
            account.vm_limit,
            account.created_at,
        ),
    )
    await db.commit()


async def get_account_by_id(db: aiosqlite.Connection, account_id: str) -> Account | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (account_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_account(row)


async def get_account_by_key(db: aiosqlite.Connection, api_key: str) -> Account | None:
    cursor = await db.execute(_SELECT + " WHERE api_key = ?", (api_key,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_account(row)
