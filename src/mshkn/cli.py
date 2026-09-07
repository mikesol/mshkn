"""`python -m mshkn`: operator commands on the configured database and the base volume."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.config import Config
from mshkn.db import connect, get_account_by_id, insert_account, list_accounts, run_migrations
from mshkn.errors import ConfigError, Conflict
from mshkn.host.dmthin import DmThinBlockStore
from mshkn.host.shell import run as shell_run
from mshkn.models import Account
from mshkn.services.base_volume import (
    BASE_DEVICE,
    BASE_IMAGE,
    DEFAULT_DOCKERFILE,
    write_base_volume,
)
from mshkn.services.recipes import docker_build_image

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

    from mshkn.host import BlockStore
    from mshkn.host.shell import RunFn
    from mshkn.services.recipes import BuildImageFn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mshkn")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations")
    base = sub.add_parser(
        "base-volume", help="build mshkn-base and write it into thin volume 0 (stop mshkn first)"
    )
    base.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    base.add_argument("--image", default=BASE_IMAGE)
    base.add_argument("--device", default=BASE_DEVICE)
    accounts = sub.add_parser("accounts", help="manage API accounts").add_subparsers(
        dest="accounts_command", required=True
    )
    create = accounts.add_parser("create", help="create an account")
    create.add_argument("--id", required=True)
    create.add_argument("--api-key", required=True)
    create.add_argument("--vm-limit", type=int, default=10)
    accounts.add_parser("list", help="list accounts (never prints keys)")
    return parser


async def base_volume(
    args: argparse.Namespace,
    config: Config,
    db: aiosqlite.Connection,
    *,
    blocks: BlockStore,
    run: RunFn = shell_run,
    build_image: BuildImageFn = docker_build_image,
) -> int:
    """The base-volume subcommand: 0 on success; 1 with the reason on stderr."""
    try:
        await write_base_volume(
            config=config,
            db=db,
            blocks=blocks,
            dockerfile=args.dockerfile,
            image_tag=args.image,
            device=args.device,
            run=run,
            build_image=build_image,
        )
    except (Conflict, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"base volume {args.device} written from {args.image}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await connect(config.db_path)
    try:
        await run_migrations(db, config.migrations_dir)
        if args.command == "migrate":
            return 0
        if args.command == "base-volume":
            blocks = DmThinBlockStore(config.thin_pool_name, config.thin_volume_sectors)
            return await base_volume(args, config, db, blocks=blocks)
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
