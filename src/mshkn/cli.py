"""`python -m mshkn`: operator commands that work directly on the configured database."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.config import Config
from mshkn.db import connect, get_account_by_id, insert_account, list_accounts, run_migrations
from mshkn.models import Account

if TYPE_CHECKING:
    from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mshkn")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations")
    accounts = sub.add_parser("accounts", help="manage API accounts").add_subparsers(
        dest="accounts_command", required=True
    )
    create = accounts.add_parser("create", help="create an account")
    create.add_argument("--id", required=True)
    create.add_argument("--api-key", required=True)
    create.add_argument("--vm-limit", type=int, default=10)
    accounts.add_parser("list", help="list accounts (never prints keys)")
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await connect(config.db_path)
    try:
        await run_migrations(db, config.migrations_dir)
        if args.command == "migrate":
            return 0
        if args.accounts_command == "list":
            for account in await list_accounts(db):
                print(f"{account.id}\t{account.vm_limit}\t{account.created_at}")
            return 0
        if await get_account_by_id(db, args.id) is not None:
            print(f"account {args.id} already exists", file=sys.stderr)
            return 1
        try:
            await insert_account(
                db,
                Account(
                    id=args.id,
                    api_key=args.api_key,
                    vm_limit=args.vm_limit,
                    created_at=datetime.now(UTC).isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:  # duplicate api_key
            print(f"cannot create account {args.id}: {exc}", file=sys.stderr)
            return 1
        print(f"created account {args.id} (vm_limit={args.vm_limit})")
        return 0
    finally:
        await db.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return asyncio.run(_run(args))
