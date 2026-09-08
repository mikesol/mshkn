"""The api_keys table (#88) and the computers.api_key_id column that records
which scoped key created a computer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import (
    delete_api_key,
    get_api_key,
    get_api_key_by_secret,
    get_computer,
    insert_account,
    insert_api_key,
    insert_computer,
    list_api_keys_by_account,
)
from mshkn.models import ApiKey, Scopes
from tests.support import account_row, computer_row

if TYPE_CHECKING:
    import aiosqlite


def _key(id: str = "key-1", *, account_id: str = "acct-1", secret: str = "mk-s-1") -> ApiKey:  # noqa: A002
    return ApiKey(
        id=id,
        account_id=account_id,
        secret=secret,
        scopes=Scopes(recipes_read=True, create_from=("rcp-1", "bare"), labels=("verb/",)),
        label="brain",
        created_at="2026-09-08T00:00:00",
    )


async def test_migration_adds_the_table_and_the_column(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    assert "api_keys" in [row[0] for row in await cursor.fetchall()]
    cursor = await db.execute("PRAGMA table_info(computers)")
    assert "api_key_id" in [row[1] for row in await cursor.fetchall()]


async def test_api_key_roundtrip(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row())
    key = _key()
    await insert_api_key(db, key)
    assert await get_api_key(db, "key-1") == key
    assert await get_api_key_by_secret(db, "mk-s-1") == key
    assert await get_api_key(db, "key-nope") is None
    assert await get_api_key_by_secret(db, "mk-nope") is None


async def test_list_is_per_account_and_delete_removes(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row())
    await insert_account(db, account_row("acct-2", api_key="other"))
    await insert_api_key(db, _key("key-a", secret="s-a"))
    await insert_api_key(db, _key("key-b", secret="s-b"))
    await insert_api_key(db, _key("key-c", account_id="acct-2", secret="s-c"))
    assert [k.id for k in await list_api_keys_by_account(db, "acct-1")] == ["key-a", "key-b"]
    await delete_api_key(db, "key-a")
    assert [k.id for k in await list_api_keys_by_account(db, "acct-1")] == ["key-b"]
    assert await get_api_key_by_secret(db, "s-a") is None


async def test_computer_records_the_key_that_created_it(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row())
    await insert_computer(db, computer_row(1, api_key_id="key-1"))
    await insert_computer(db, computer_row(2))
    scoped = await get_computer(db, "comp-1")
    account = await get_computer(db, "comp-2")
    assert scoped is not None and scoped.api_key_id == "key-1"
    assert account is not None and account.api_key_id is None
