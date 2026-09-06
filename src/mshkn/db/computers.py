"""computers table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import Computer, ComputerStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "thin_volume_id",
    "tap_device",
    "vm_ip",
    "socket_path",
    "firecracker_pid",
    "status",
    "created_at",
    "last_exec_at",
    "source_checkpoint_id",
    "recipe_id",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM computers"


def _row_to_computer(row: Sequence[object]) -> Computer:
    d = dict(zip(COLUMNS, row, strict=True))
    return Computer(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        thin_volume_id=int(d["thin_volume_id"]),  # type: ignore[call-overload]
        tap_device=str(d["tap_device"]),
        vm_ip=str(d["vm_ip"]),
        socket_path=str(d["socket_path"]),
        firecracker_pid=None if d["firecracker_pid"] is None else int(d["firecracker_pid"]),  # type: ignore[call-overload]
        status=ComputerStatus(str(d["status"])),
        created_at=str(d["created_at"]),
        last_exec_at=None if d["last_exec_at"] is None else str(d["last_exec_at"]),
        source_checkpoint_id=(
            None if d["source_checkpoint_id"] is None else str(d["source_checkpoint_id"])
        ),
        recipe_id=None if d["recipe_id"] is None else str(d["recipe_id"]),
    )


async def insert_computer(db: aiosqlite.Connection, computer: Computer) -> None:
    await db.execute(
        "INSERT INTO computers (" + ", ".join(COLUMNS) + ", manifest_hash, manifest_json) "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ", '', '{}')",
        (
            computer.id,
            computer.account_id,
            computer.thin_volume_id,
            computer.tap_device,
            computer.vm_ip,
            computer.socket_path,
            computer.firecracker_pid,
            computer.status,
            computer.created_at,
            computer.last_exec_at,
            computer.source_checkpoint_id,
            computer.recipe_id,
        ),
    )
    await db.commit()


async def count_active_computers(db: aiosqlite.Connection) -> int:
    """Count non-destroyed computers across every account (feeds the gauge)."""
    cursor = await db.execute("SELECT COUNT(*) FROM computers WHERE status != 'destroyed'")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def get_computer(db: aiosqlite.Connection, computer_id: str) -> Computer | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (computer_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_computer(row)


async def list_all_computers(db: aiosqlite.Connection) -> list[Computer]:
    """Return all non-destroyed computers across all accounts."""
    cursor = await db.execute(_SELECT + " WHERE status != 'destroyed'")
    return [_row_to_computer(r) for r in await cursor.fetchall()]


async def count_active_computers_by_account(db: aiosqlite.Connection, account_id: str) -> int:
    """Count non-destroyed computers for the given account."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM computers WHERE account_id = ? AND status != 'destroyed'",
        (account_id,),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def update_computer_status(
    db: aiosqlite.Connection, computer_id: str, status: ComputerStatus
) -> None:
    await db.execute("UPDATE computers SET status = ? WHERE id = ?", (status, computer_id))
    await db.commit()


async def update_last_exec_at(db: aiosqlite.Connection, computer_id: str, timestamp: str) -> None:
    await db.execute("UPDATE computers SET last_exec_at = ? WHERE id = ?", (timestamp, computer_id))
    await db.commit()


async def get_active_computer_for_label(
    db: aiosqlite.Connection, account_id: str, label: str
) -> Computer | None:
    """Return a running computer whose source checkpoint has the given label, or None."""
    cols = ", ".join("c." + c for c in COLUMNS)
    cursor = await db.execute(
        f"SELECT {cols} FROM computers c "
        "INNER JOIN checkpoints ck ON c.source_checkpoint_id = ck.id "
        "WHERE c.account_id = ? AND c.status = 'running' AND ck.label = ? LIMIT 1",
        (account_id, label),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_computer(row)
