from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import Account, Checkpoint, Computer

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite


async def run_migrations(db: aiosqlite.Connection, migrations_dir: Path) -> None:
    # Ensure _migrations table exists (bootstrap — first migration also creates it,
    # but we need to check it before we can query it)
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
        sql = sql_file.read_text()
        # Skip the _migrations CREATE in the migration file since we already have it
        for statement in sql.split(";"):
            stmt = statement.strip()
            if stmt and "CREATE TABLE _migrations" not in stmt:
                await db.execute(stmt)
        await db.execute("INSERT INTO _migrations (filename) VALUES (?)", (sql_file.name,))
        await db.commit()


async def insert_account(db: aiosqlite.Connection, account: Account) -> None:
    await db.execute(
        "INSERT INTO accounts (id, api_key, vm_limit, created_at) VALUES (?, ?, ?, ?)",
        (account.id, account.api_key, account.vm_limit, account.created_at),
    )
    await db.commit()


async def get_account_by_key(db: aiosqlite.Connection, api_key: str) -> Account | None:
    cursor = await db.execute(
        "SELECT id, api_key, vm_limit, created_at FROM accounts WHERE api_key = ?",
        (api_key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Account(id=row[0], api_key=row[1], vm_limit=row[2], created_at=row[3])


async def insert_computer(db: aiosqlite.Connection, computer: Computer) -> None:
    await db.execute(
        "INSERT INTO computers "
        "(id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            computer.id,
            computer.account_id,
            computer.thin_volume_id,
            computer.tap_device,
            computer.vm_ip,
            computer.socket_path,
            computer.firecracker_pid,
            computer.manifest_hash,
            computer.status,
            computer.created_at,
            computer.last_exec_at,
        ),
    )
    await db.commit()


async def get_computer(db: aiosqlite.Connection, computer_id: str) -> Computer | None:
    cursor = await db.execute(
        "SELECT id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at "
        "FROM computers WHERE id = ?",
        (computer_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Computer(
        id=row[0],
        account_id=row[1],
        thin_volume_id=row[2],
        tap_device=row[3],
        vm_ip=row[4],
        socket_path=row[5],
        firecracker_pid=row[6],
        manifest_hash=row[7],
        status=row[8],
        created_at=row[9],
        last_exec_at=row[10],
    )


async def list_all_computers(db: aiosqlite.Connection) -> list[Computer]:
    """Return all non-destroyed computers across all accounts."""
    cursor = await db.execute(
        "SELECT id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at "
        "FROM computers WHERE status != 'destroyed'",
    )
    rows = await cursor.fetchall()
    return [
        Computer(
            id=r[0],
            account_id=r[1],
            thin_volume_id=r[2],
            tap_device=r[3],
            vm_ip=r[4],
            socket_path=r[5],
            firecracker_pid=r[6],
            manifest_hash=r[7],
            status=r[8],
            created_at=r[9],
            last_exec_at=r[10],
        )
        for r in rows
    ]


async def list_computers_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[Computer]:
    cursor = await db.execute(
        "SELECT id, account_id, thin_volume_id, tap_device, vm_ip, socket_path, "
        "firecracker_pid, manifest_hash, status, created_at, last_exec_at "
        "FROM computers WHERE account_id = ? AND status != 'destroyed'",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        Computer(
            id=r[0],
            account_id=r[1],
            thin_volume_id=r[2],
            tap_device=r[3],
            vm_ip=r[4],
            socket_path=r[5],
            firecracker_pid=r[6],
            manifest_hash=r[7],
            status=r[8],
            created_at=r[9],
            last_exec_at=r[10],
        )
        for r in rows
    ]


async def update_computer_status(
    db: aiosqlite.Connection, computer_id: str, status: str
) -> None:
    await db.execute(
        "UPDATE computers SET status = ? WHERE id = ?",
        (status, computer_id),
    )
    await db.commit()


async def insert_checkpoint(db: aiosqlite.Connection, checkpoint: Checkpoint) -> None:
    await db.execute(
        "INSERT INTO checkpoints "
        "(id, account_id, parent_id, computer_id, thin_volume_id, manifest_hash, manifest_json, "
        "r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, pinned, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            checkpoint.id,
            checkpoint.account_id,
            checkpoint.parent_id,
            checkpoint.computer_id,
            checkpoint.thin_volume_id,
            checkpoint.manifest_hash,
            checkpoint.manifest_json,
            checkpoint.r2_prefix,
            checkpoint.disk_delta_size_bytes,
            checkpoint.memory_size_bytes,
            checkpoint.label,
            int(checkpoint.pinned),
            checkpoint.created_at,
        ),
    )
    await db.commit()


async def get_checkpoint(db: aiosqlite.Connection, checkpoint_id: str) -> Checkpoint | None:
    cursor = await db.execute(
        "SELECT id, account_id, parent_id, computer_id, thin_volume_id, manifest_hash, "
        "manifest_json, r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, "
        "pinned, created_at "
        "FROM checkpoints WHERE id = ?",
        (checkpoint_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return Checkpoint(
        id=row[0],
        account_id=row[1],
        parent_id=row[2],
        computer_id=row[3],
        thin_volume_id=row[4],
        manifest_hash=row[5],
        manifest_json=row[6],
        r2_prefix=row[7],
        disk_delta_size_bytes=row[8],
        memory_size_bytes=row[9],
        label=row[10],
        pinned=bool(row[11]),
        created_at=row[12],
    )


async def list_checkpoints_by_account(
    db: aiosqlite.Connection, account_id: str
) -> list[Checkpoint]:
    cursor = await db.execute(
        "SELECT id, account_id, parent_id, computer_id, thin_volume_id, manifest_hash, "
        "manifest_json, r2_prefix, disk_delta_size_bytes, memory_size_bytes, label, "
        "pinned, created_at "
        "FROM checkpoints WHERE account_id = ? ORDER BY created_at DESC",
        (account_id,),
    )
    rows = await cursor.fetchall()
    return [
        Checkpoint(
            id=r[0],
            account_id=r[1],
            parent_id=r[2],
            computer_id=r[3],
            thin_volume_id=r[4],
            manifest_hash=r[5],
            manifest_json=r[6],
            r2_prefix=r[7],
            disk_delta_size_bytes=r[8],
            memory_size_bytes=r[9],
            label=r[10],
            pinned=bool(r[11]),
            created_at=r[12],
        )
        for r in rows
    ]


async def get_max_checkpoint_volume_id(db: aiosqlite.Connection) -> int | None:
    """Return the highest thin_volume_id across all checkpoints, or None."""
    cursor = await db.execute(
        "SELECT MAX(thin_volume_id) FROM checkpoints WHERE thin_volume_id IS NOT NULL"
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else None


async def delete_checkpoint(db: aiosqlite.Connection, checkpoint_id: str) -> None:
    await db.execute("DELETE FROM checkpoints WHERE id = ?", (checkpoint_id,))
    await db.commit()
