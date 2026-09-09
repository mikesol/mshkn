"""The service's one connection never stays inside a transaction (#105).

Two connections play the incident: A is the service's, opened by `connect`;
B is any other process (litestream commits once a second). A write on A that
loses the lock to B must not leave A in a transaction, or A's next read pins
a snapshot and every later write on A is refused the moment B commits.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from mshkn.db import (
    connect,
    get_computer,
    insert_account,
    insert_computer,
    run_migrations,
    update_computer_status,
)
from mshkn.models import ComputerStatus
from tests.support import account_row, computer_row

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def pair(tmp_path: Path) -> AsyncIterator[tuple[aiosqlite.Connection, aiosqlite.Connection]]:
    path = tmp_path / "t.db"
    a = await connect(path)
    await run_migrations(a, Path("migrations"))
    await a.execute("PRAGMA busy_timeout=50")  # the contended write gives up quickly
    await insert_account(a, account_row("acct-1", api_key="k"))
    await insert_computer(a, computer_row(1, id="comp-1", account_id="acct-1"))
    b = await aiosqlite.connect(path)
    try:
        yield a, b
    finally:
        await b.close()
        await a.close()


async def _lose_a_write_to_b(a: aiosqlite.Connection, b: aiosqlite.Connection) -> None:
    await b.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await update_computer_status(a, "comp-1", ComputerStatus.DESTROYING)
    await b.execute("ROLLBACK")


async def test_a_failed_write_leaves_no_open_transaction(
    pair: tuple[aiosqlite.Connection, aiosqlite.Connection],
) -> None:
    a, b = pair
    await _lose_a_write_to_b(a, b)
    assert a.in_transaction is False


async def test_writes_succeed_after_another_connection_commits(
    pair: tuple[aiosqlite.Connection, aiosqlite.Connection],
) -> None:
    """The chain from the live host: a lost write, a read, an outside commit, the next write."""
    a, b = pair
    await _lose_a_write_to_b(a, b)
    assert await get_computer(a, "comp-1") is not None
    await b.execute("UPDATE computers SET last_exec_at = 'x' WHERE id = 'comp-1'")
    await b.commit()
    await update_computer_status(a, "comp-1", ComputerStatus.DESTROYING)
    cursor = await b.execute("SELECT status FROM computers WHERE id = 'comp-1'")
    assert await cursor.fetchone() == ("destroying",)


async def test_a_write_is_durable_without_a_commit_call(
    pair: tuple[aiosqlite.Connection, aiosqlite.Connection],
) -> None:
    a, b = pair
    await update_computer_status(a, "comp-1", ComputerStatus.DESTROYING)
    assert a.in_transaction is False
    cursor = await b.execute("SELECT status FROM computers WHERE id = 'comp-1'")
    assert await cursor.fetchone() == ("destroying",)


async def test_a_failed_migration_applies_nothing(tmp_path: Path) -> None:
    """A migration is one transaction: its script and its ledger row land together or not at all."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_good.sql").write_text("CREATE TABLE good (x INTEGER);")
    (migrations / "002_bad.sql").write_text(
        "CREATE TABLE half (x INTEGER);\nCREATE TABLE half (x INTEGER);"
    )
    db = await connect(tmp_path / "t.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            await run_migrations(db, migrations)
        assert db.in_transaction is False
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE name IN ('good', 'half')")
        assert [row[0] for row in await cursor.fetchall()] == ["good"]
        cursor = await db.execute("SELECT filename FROM _migrations")
        assert [row[0] for row in await cursor.fetchall()] == ["001_good.sql"]
    finally:
        await db.close()
