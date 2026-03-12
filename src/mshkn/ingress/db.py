from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.ingress.models import IngressLog, IngressRule

if TYPE_CHECKING:
    import aiosqlite


async def insert_ingress_rule(db: aiosqlite.Connection, rule: IngressRule) -> None:
    await db.execute(
        "INSERT INTO ingress_rules "
        "(internal_id, id, account_id, name, starlark_source, response_mode, "
        "max_body_bytes, rate_limit_rpm, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
    await db.commit()


async def get_ingress_rule_by_id(
    db: aiosqlite.Connection, rule_id: str
) -> IngressRule | None:
    cursor = await db.execute(
        "SELECT internal_id, id, account_id, name, starlark_source, response_mode, "
        "max_body_bytes, rate_limit_rpm, enabled, created_at, updated_at "
        "FROM ingress_rules WHERE id = ?",
        (rule_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return IngressRule(
        internal_id=row[0],
        id=row[1],
        account_id=row[2],
        name=row[3],
        starlark_source=row[4],
        response_mode=row[5],
        max_body_bytes=row[6],
        rate_limit_rpm=row[7],
        enabled=bool(row[8]),
        created_at=row[9],
        updated_at=row[10],
    )


async def list_ingress_rules_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[IngressRule]:
    cursor = await db.execute(
        "SELECT internal_id, id, account_id, name, starlark_source, response_mode, "
        "max_body_bytes, rate_limit_rpm, enabled, created_at, updated_at "
        "FROM ingress_rules WHERE account_id = ? ORDER BY created_at",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        IngressRule(
            internal_id=r[0],
            id=r[1],
            account_id=r[2],
            name=r[3],
            starlark_source=r[4],
            response_mode=r[5],
            max_body_bytes=r[6],
            rate_limit_rpm=r[7],
            enabled=bool(r[8]),
            created_at=r[9],
            updated_at=r[10],
        )
        for r in rows
    ]


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
    await db.commit()


async def rotate_ingress_rule_id(
    db: aiosqlite.Connection, internal_id: str, new_id: str
) -> None:
    await db.execute(
        "UPDATE ingress_rules SET id=?, updated_at=datetime('now') WHERE internal_id=?",
        (new_id, internal_id),
    )
    await db.commit()


async def delete_ingress_rule(db: aiosqlite.Connection, rule_id: str) -> None:
    # Get internal_id first to cascade-delete logs
    cursor = await db.execute(
        "SELECT internal_id FROM ingress_rules WHERE id=?", (rule_id,)
    )
    row = await cursor.fetchone()
    if row:
        await db.execute("DELETE FROM ingress_log WHERE rule_internal_id=?", (row[0],))
        await db.execute("DELETE FROM ingress_rules WHERE id=?", (rule_id,))
        await db.commit()


async def insert_ingress_log(db: aiosqlite.Connection, log: IngressLog) -> None:
    await db.execute(
        "INSERT INTO ingress_log (id, rule_internal_id, status, starlark_result, "
        "error_message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            log.id,
            log.rule_internal_id,
            log.status,
            log.starlark_result,
            log.error_message,
            log.created_at,
        ),
    )
    await db.commit()


async def list_ingress_logs(
    db: aiosqlite.Connection, rule_internal_id: str, limit: int = 100
) -> list[IngressLog]:
    cursor = await db.execute(
        "SELECT id, rule_internal_id, status, starlark_result, error_message, created_at "
        "FROM ingress_log WHERE rule_internal_id=? ORDER BY created_at DESC LIMIT ?",
        (rule_internal_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        IngressLog(
            id=r[0],
            rule_internal_id=r[1],
            status=r[2],
            starlark_result=r[3],
            error_message=r[4],
            created_at=r[5],
        )
        for r in rows
    ]


async def prune_old_ingress_logs(
    db: aiosqlite.Connection, before_timestamp: str
) -> int:
    cursor = await db.execute(
        "DELETE FROM ingress_log WHERE created_at < ?", (before_timestamp,)
    )
    await db.commit()
    return cursor.rowcount
