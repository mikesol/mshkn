"""relay_jobs table: the relay's jobs (#110), headers in their own column."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mshkn.models import DeliveryStatus, RelayDelivery, RelayJob, RelayStatus, RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "api_key_id",
    "status",
    "target",
    "method",
    "forward_headers_json",
    "body_json",
    "retry_json",
    "timeout_seconds",
    "deliver_json",
    "attempts",
    "error",
    "response_status",
    "response_headers_json",
    "response_body_json",
    "delivery_status",
    "delivery_attempts",
    "delivery_computer_id",
    "delivery_deferred_id",
    "delivery_error",
    "created_at",
    "updated_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM relay_jobs"
# Everything but the id, written whole by update_relay_job.
_MUTABLE = COLUMNS[1:]


def _loads(value: object) -> object:
    return None if value is None else json.loads(str(value))


def _dumps(value: object) -> str | None:
    return None if value is None else json.dumps(value)


def _values(job: RelayJob) -> tuple[object, ...]:
    return (
        job.id,
        job.account_id,
        job.api_key_id,
        str(job.status),
        job.target,
        job.method,
        _dumps(job.forward_headers),
        _dumps(job.body),
        json.dumps(job.retry.to_document()),
        job.timeout_seconds,
        None
        if job.deliver is None
        else json.dumps({"label": job.deliver.label, "exec": job.deliver.exec}),
        job.attempts,
        job.error,
        job.response_status,
        _dumps(job.response_headers),
        _dumps(job.response_body),
        None if job.delivery_status is None else str(job.delivery_status),
        job.delivery_attempts,
        job.delivery_computer_id,
        job.delivery_deferred_id,
        job.delivery_error,
        job.created_at,
        job.updated_at,
    )


def _row_to_job(row: Sequence[object]) -> RelayJob:
    d = dict(zip(COLUMNS, row, strict=True))
    retry = json.loads(str(d["retry_json"]))
    deliver = _loads(d["deliver_json"])
    return RelayJob(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        api_key_id=None if d["api_key_id"] is None else str(d["api_key_id"]),
        status=RelayStatus(str(d["status"])),
        target=str(d["target"]),
        method=str(d["method"]),
        forward_headers=_loads(d["forward_headers_json"]),  # type: ignore[arg-type]
        body=_loads(d["body_json"]),
        retry=RetryPolicy(
            attempts=int(retry["attempts"]),
            initial_delay_ms=int(retry["initial_delay_ms"]),
            max_delay_ms=int(retry["max_delay_ms"]),
        ),
        timeout_seconds=int(d["timeout_seconds"]),  # type: ignore[call-overload]
        deliver=None
        if deliver is None
        else RelayDelivery(label=str(deliver["label"]), exec=str(deliver["exec"])),  # type: ignore[index]
        attempts=int(d["attempts"]),  # type: ignore[call-overload]
        error=None if d["error"] is None else str(d["error"]),
        response_status=None if d["response_status"] is None else int(d["response_status"]),  # type: ignore[call-overload]
        response_headers=_loads(d["response_headers_json"]),  # type: ignore[arg-type]
        response_body=_loads(d["response_body_json"]),
        delivery_status=None
        if d["delivery_status"] is None
        else DeliveryStatus(str(d["delivery_status"])),
        delivery_attempts=int(d["delivery_attempts"]),  # type: ignore[call-overload]
        delivery_computer_id=None
        if d["delivery_computer_id"] is None
        else str(d["delivery_computer_id"]),
        delivery_deferred_id=None
        if d["delivery_deferred_id"] is None
        else str(d["delivery_deferred_id"]),
        delivery_error=None if d["delivery_error"] is None else str(d["delivery_error"]),
        created_at=str(d["created_at"]),
        updated_at=str(d["updated_at"]),
    )


async def insert_relay_job(db: aiosqlite.Connection, job: RelayJob) -> None:
    await db.execute(
        "INSERT INTO relay_jobs (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        _values(job),
    )


async def update_relay_job(db: aiosqlite.Connection, job: RelayJob) -> None:
    """Write every column but the id from the dataclass: one statement, one shape."""
    await db.execute(
        "UPDATE relay_jobs SET " + ", ".join(f"{c} = ?" for c in _MUTABLE) + " WHERE id = ?",
        (*_values(job)[1:], job.id),
    )


async def get_relay_job(db: aiosqlite.Connection, job_id: str) -> RelayJob | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_job(row)


async def list_relay_jobs_by_status(
    db: aiosqlite.Connection, statuses: Iterable[RelayStatus]
) -> list[RelayJob]:
    wanted = [str(s) for s in statuses]
    cursor = await db.execute(
        _SELECT
        + " WHERE status IN ("
        + ", ".join("?" for _ in wanted)
        + ") ORDER BY created_at, id",
        wanted,
    )
    return [_row_to_job(r) for r in await cursor.fetchall()]


_SETTLED = (str(RelayStatus.COMPLETED), str(RelayStatus.FAILED))


async def delete_relay_jobs_before(db: aiosqlite.Connection, cutoff: str) -> int:
    """Delete settled jobs (completed or failed) created before the ISO-8601 cutoff.

    A job still queued or in progress survives regardless of age: retention reclaims
    finished records, it does not destroy work in progress (#114 review).
    """
    cursor = await db.execute(
        "DELETE FROM relay_jobs WHERE created_at < ? AND status IN (?, ?)",
        (cutoff, *_SETTLED),
    )
    return cursor.rowcount
