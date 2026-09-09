"""ingress_rules and ingress_log tables."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import IngressLog, IngressLogStatus, IngressRule

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "internal_id",
    "id",
    "account_id",
    "name",
    "starlark_source",
    "response_mode",
    "max_body_bytes",
    "rate_limit_rpm",
    "enabled",
    "created_at",
    "updated_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM ingress_rules"

LOG_COLUMNS: tuple[str, ...] = (
    "id",
    "rule_internal_id",
    "status",
    "starlark_result",
    "error_message",
    "created_at",
    "computer_id",
)
_LOG_SELECT = "SELECT " + ", ".join(LOG_COLUMNS) + " FROM ingress_log"


def _row_to_rule(row: Sequence[object]) -> IngressRule:
    d = dict(zip(COLUMNS, row, strict=True))
    return IngressRule(
        internal_id=str(d["internal_id"]),
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        name=str(d["name"]),
        starlark_source=str(d["starlark_source"]),
        response_mode=str(d["response_mode"]),
        max_body_bytes=int(d["max_body_bytes"]),  # type: ignore[call-overload]
        rate_limit_rpm=int(d["rate_limit_rpm"]),  # type: ignore[call-overload]
        enabled=bool(d["enabled"]),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
    )


def _row_to_log(row: Sequence[object]) -> IngressLog:
    d = dict(zip(LOG_COLUMNS, row, strict=True))
    return IngressLog(
        id=str(d["id"]),
        rule_internal_id=str(d["rule_internal_id"]),
        status=IngressLogStatus(str(d["status"])),
        starlark_result=None if d["starlark_result"] is None else str(d["starlark_result"]),
        error_message=None if d["error_message"] is None else str(d["error_message"]),
        created_at=str(d["created_at"]),
        computer_id=None if d["computer_id"] is None else str(d["computer_id"]),
    )


async def insert_ingress_rule(db: aiosqlite.Connection, rule: IngressRule) -> None:
    await db.execute(
        "INSERT INTO ingress_rules (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (
            rule.internal_id,
            rule.id,
            rule.account_id,
            rule.name,
            rule.starlark_source,
            rule.response_mode,
            rule.max_body_bytes,
            rule.rate_limit_rpm,
            1 if rule.enabled else 0,
            rule.created_at,
            rule.updated_at,
        ),
    )


async def get_ingress_rule_by_id(db: aiosqlite.Connection, rule_id: str) -> IngressRule | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (rule_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_rule(row)


async def list_ingress_rules_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[IngressRule]:
    cursor = await db.execute(
        _SELECT + " WHERE account_id = ? ORDER BY created_at",
        (account_id,),
    )
    return [_row_to_rule(r) for r in await cursor.fetchall()]


async def update_ingress_rule(db: aiosqlite.Connection, rule: IngressRule) -> None:
    await db.execute(
        "UPDATE ingress_rules SET name=?, starlark_source=?, response_mode=?, "
        "max_body_bytes=?, rate_limit_rpm=?, enabled=?, updated_at=? "
        "WHERE internal_id=?",
        (
            rule.name,
            rule.starlark_source,
            rule.response_mode,
            rule.max_body_bytes,
            rule.rate_limit_rpm,
            1 if rule.enabled else 0,
            rule.updated_at,
            rule.internal_id,
        ),
    )


async def rotate_ingress_rule_id(db: aiosqlite.Connection, internal_id: str, new_id: str) -> None:
    await db.execute(
        "UPDATE ingress_rules SET id=?, updated_at=datetime('now') WHERE internal_id=?",
        (new_id, internal_id),
    )


async def delete_ingress_rule(db: aiosqlite.Connection, rule_id: str) -> None:
    """Delete a rule and its log. Two statements, log first: a failure between them
    leaves a rule whose next delete converges, never log rows without a rule."""
    await db.execute(
        "DELETE FROM ingress_log WHERE rule_internal_id IN "
        "(SELECT internal_id FROM ingress_rules WHERE id = ?)",
        (rule_id,),
    )
    await db.execute("DELETE FROM ingress_rules WHERE id = ?", (rule_id,))


async def insert_ingress_log(db: aiosqlite.Connection, log: IngressLog) -> None:
    await db.execute(
        "INSERT INTO ingress_log (" + ", ".join(LOG_COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in LOG_COLUMNS) + ")",
        (
            log.id,
            log.rule_internal_id,
            log.status,
            log.starlark_result,
            log.error_message,
            log.created_at,
            log.computer_id,
        ),
    )


async def update_ingress_log(
    db: aiosqlite.Connection,
    log_id: str,
    *,
    status: IngressLogStatus,
    error_message: str | None,
    computer_id: str | None,
) -> None:
    """Record how an accepted trigger ended: the same row, its final status."""
    await db.execute(
        "UPDATE ingress_log SET status=?, error_message=?, computer_id=? WHERE id=?",
        (status, error_message, computer_id, log_id),
    )


async def list_ingress_logs(
    db: aiosqlite.Connection, rule_internal_id: str, limit: int = 100
) -> list[IngressLog]:
    cursor = await db.execute(
        _LOG_SELECT + " WHERE rule_internal_id=? ORDER BY created_at DESC LIMIT ?",
        (rule_internal_id, limit),
    )
    return [_row_to_log(r) for r in await cursor.fetchall()]
