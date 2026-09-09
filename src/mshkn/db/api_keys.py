"""api_keys table: the scoped keys of #88."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mshkn.models import ApiKey, parse_scopes

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "secret",
    "scopes_json",
    "label",
    "created_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM api_keys"


def _row_to_api_key(row: Sequence[object]) -> ApiKey:
    d = dict(zip(COLUMNS, row, strict=True))
    return ApiKey(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        secret=str(d["secret"]),
        scopes=parse_scopes(json.loads(str(d["scopes_json"]))),
        label=None if d["label"] is None else str(d["label"]),
        created_at=str(d["created_at"]),
    )


async def insert_api_key(db: aiosqlite.Connection, key: ApiKey) -> None:
    await db.execute(
        "INSERT INTO api_keys (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (
            key.id,
            key.account_id,
            key.secret,
            json.dumps(key.scopes.to_document()),
            key.label,
            key.created_at,
        ),
    )


async def get_api_key(db: aiosqlite.Connection, key_id: str) -> ApiKey | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (key_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_api_key(row)


async def get_api_key_by_secret(db: aiosqlite.Connection, secret: str) -> ApiKey | None:
    cursor = await db.execute(_SELECT + " WHERE secret = ?", (secret,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_api_key(row)


async def list_api_keys_by_account(db: aiosqlite.Connection, account_id: str) -> list[ApiKey]:
    cursor = await db.execute(
        _SELECT + " WHERE account_id = ? ORDER BY created_at, id", (account_id,)
    )
    return [_row_to_api_key(r) for r in await cursor.fetchall()]


async def delete_api_key(db: aiosqlite.Connection, key_id: str) -> None:
    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
