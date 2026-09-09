"""exec_log table: the output of an ephemeral run, kept after its computer is gone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import ExecLog

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "computer_id",
    "account_id",
    "source_checkpoint_id",
    "created_checkpoint_id",
    "label",
    "command",
    "exit_code",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "created_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM exec_log"


def _row_to_exec_log(row: Sequence[object]) -> ExecLog:
    d = dict(zip(COLUMNS, row, strict=True))
    return ExecLog(
        computer_id=str(d["computer_id"]),
        account_id=str(d["account_id"]),
        source_checkpoint_id=(
            None if d["source_checkpoint_id"] is None else str(d["source_checkpoint_id"])
        ),
        created_checkpoint_id=(
            None if d["created_checkpoint_id"] is None else str(d["created_checkpoint_id"])
        ),
        label=None if d["label"] is None else str(d["label"]),
        command=str(d["command"]),
        exit_code=int(d["exit_code"]),  # type: ignore[call-overload]
        stdout=str(d["stdout"]),
        stderr=str(d["stderr"]),
        stdout_truncated=bool(d["stdout_truncated"]),
        stderr_truncated=bool(d["stderr_truncated"]),
        created_at=str(d["created_at"]),
    )


async def insert_exec_log(db: aiosqlite.Connection, log: ExecLog) -> None:
    await db.execute(
        "INSERT INTO exec_log (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (
            log.computer_id,
            log.account_id,
            log.source_checkpoint_id,
            log.created_checkpoint_id,
            log.label,
            log.command,
            log.exit_code,
            log.stdout,
            log.stderr,
            int(log.stdout_truncated),
            int(log.stderr_truncated),
            log.created_at,
        ),
    )


async def set_exec_log_checkpoint(
    db: aiosqlite.Connection, computer_id: str, checkpoint_id: str
) -> None:
    await db.execute(
        "UPDATE exec_log SET created_checkpoint_id = ? WHERE computer_id = ?",
        (checkpoint_id, computer_id),
    )


async def get_exec_log(db: aiosqlite.Connection, computer_id: str) -> ExecLog | None:
    cursor = await db.execute(_SELECT + " WHERE computer_id = ?", (computer_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_exec_log(row)


async def delete_exec_logs_before(db: aiosqlite.Connection, cutoff: str) -> int:
    """Delete every row created before the ISO-8601 cutoff; return how many went."""
    cursor = await db.execute("DELETE FROM exec_log WHERE created_at < ?", (cutoff,))
    return cursor.rowcount
