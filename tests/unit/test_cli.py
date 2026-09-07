from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mshkn.cli import main
from mshkn.db import connect, get_account_by_id

if TYPE_CHECKING:
    from mshkn.models import Account


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("MSHKN_DB_PATH", str(db_path))
    monkeypatch.setenv("MSHKN_MIGRATIONS_DIR", str(Path("migrations").resolve()))
    return db_path


async def _fetch_account(db_path: Path, account_id: str) -> Account | None:
    db = await connect(db_path)
    try:
        return await get_account_by_id(db, account_id)
    finally:
        await db.close()


def test_accounts_create_and_list(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A plain (non-async) test: main() owns its own event loop via asyncio.run(),
    # which cannot nest inside the loop pytest-asyncio would otherwise be
    # running this test on. The DB read below borrows asyncio.run() too, once
    # main() has returned and released its loop.
    argv = ["accounts", "create", "--id", "acct-x", "--api-key", "secret", "--vm-limit", "7"]
    assert main(argv) == 0
    account = asyncio.run(_fetch_account(env, "acct-x"))
    assert account is not None and account.api_key == "secret" and account.vm_limit == 7
    assert main(["accounts", "list"]) == 0
    out = capsys.readouterr().out
    assert "acct-x\t7\t" in out and "secret" not in out


def test_accounts_create_twice_fails_cleanly(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["accounts", "create", "--id", "acct-x", "--api-key", "k"]) == 0
    assert main(["accounts", "create", "--id", "acct-x", "--api-key", "k2"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_migrate_is_idempotent(env: Path) -> None:
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    assert env.exists()


def test_accounts_create_rejects_a_duplicate_api_key(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["accounts", "create", "--id", "a", "--api-key", "shared"]) == 0
    assert main(["accounts", "create", "--id", "b", "--api-key", "shared"]) == 1
    assert "cannot create account b" in capsys.readouterr().err
