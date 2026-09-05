"""SQLite access: connection setup, migrations, and one module per table.

Every query function is importable from this package so callers do not need
to know which table module owns it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from mshkn.db.accounts import get_account_by_id, get_account_by_key, insert_account
from mshkn.db.checkpoints import (
    delete_checkpoint,
    get_checkpoint,
    get_latest_checkpoint_for_computer,
    get_max_checkpoint_volume_id,
    insert_checkpoint,
    list_account_ids_with_checkpoints,
    list_checkpoints_by_account,
    list_prunable_checkpoints,
)
from mshkn.db.computers import (
    count_active_computers_by_account,
    get_active_computer_for_label,
    get_computer,
    insert_computer,
    list_all_computers,
    update_computer_status,
    update_last_exec_at,
)
from mshkn.db.deferred import delete_deferred_by_label, insert_deferred, list_deferred_by_label
from mshkn.db.ingress import (
    delete_ingress_rule,
    get_ingress_rule_by_id,
    insert_ingress_log,
    insert_ingress_rule,
    list_ingress_logs,
    list_ingress_rules_by_account,
    prune_old_ingress_logs,
    rotate_ingress_rule_id,
    update_ingress_rule,
)
from mshkn.db.recipes import (
    count_recipe_references,
    delete_failed_recipes_by_hash,
    delete_recipe,
    get_max_recipe_volume_id,
    get_recipe,
    get_recipe_by_content_hash,
    insert_recipe,
    list_recipes_by_account,
    update_recipe_build_result,
    update_recipe_status,
    update_recipe_template,
)
from mshkn.db.templates import cache_bare_template, get_bare_template

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "cache_bare_template",
    "connect",
    "count_active_computers_by_account",
    "count_recipe_references",
    "delete_checkpoint",
    "delete_deferred_by_label",
    "delete_failed_recipes_by_hash",
    "delete_ingress_rule",
    "delete_recipe",
    "get_account_by_id",
    "get_account_by_key",
    "get_active_computer_for_label",
    "get_bare_template",
    "get_checkpoint",
    "get_computer",
    "get_ingress_rule_by_id",
    "get_latest_checkpoint_for_computer",
    "get_max_checkpoint_volume_id",
    "get_max_recipe_volume_id",
    "get_recipe",
    "get_recipe_by_content_hash",
    "insert_account",
    "insert_checkpoint",
    "insert_computer",
    "insert_deferred",
    "insert_ingress_log",
    "insert_ingress_rule",
    "insert_recipe",
    "list_account_ids_with_checkpoints",
    "list_all_computers",
    "list_checkpoints_by_account",
    "list_deferred_by_label",
    "list_ingress_logs",
    "list_ingress_rules_by_account",
    "list_prunable_checkpoints",
    "list_recipes_by_account",
    "prune_old_ingress_logs",
    "rotate_ingress_rule_id",
    "run_migrations",
    "update_computer_status",
    "update_ingress_rule",
    "update_last_exec_at",
    "update_recipe_build_result",
    "update_recipe_status",
    "update_recipe_template",
]


async def connect(path: Path | str) -> aiosqlite.Connection:
    """Open the database with the pragmas the service relies on.

    WAL for concurrent readers and Litestream; NORMAL sync is durable enough
    under WAL and much faster; a busy timeout so concurrent writers wait
    instead of failing. Foreign keys stay OFF on purpose: the schema has
    REFERENCES without ON DELETE actions and destroyed rows are retained, so
    enforcement would break checkpoint deletion and pruning.
    """
    db = await aiosqlite.connect(path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def run_migrations(db: aiosqlite.Connection, migrations_dir: Path) -> None:
    """Apply every *.sql file in name order that is not yet recorded in _migrations."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS _migrations "
        "(id INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    await db.commit()
    cursor = await db.execute("SELECT filename FROM _migrations")
    applied = {row[0] for row in await cursor.fetchall()}
    for sql_file in sorted(migrations_dir.glob("*.sql")):
        if sql_file.name in applied:
            continue
        # 001 creates _migrations itself; we already did, so make it idempotent.
        sql = sql_file.read_text().replace(
            "CREATE TABLE _migrations", "CREATE TABLE IF NOT EXISTS _migrations"
        )
        await db.executescript(sql)
        await db.execute("INSERT INTO _migrations (filename) VALUES (?)", (sql_file.name,))
        await db.commit()
