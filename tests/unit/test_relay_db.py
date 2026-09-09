"""relay_jobs rows round-trip; headers are a column of their own; expiry is by created_at."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import (
    delete_relay_jobs_before,
    get_relay_job,
    insert_account,
    insert_relay_job,
    list_relay_jobs_by_status,
    update_relay_job,
)
from mshkn.models import DeliveryStatus, RelayDelivery, RelayJob, RelayStatus, RetryPolicy
from tests.support import account_row

if TYPE_CHECKING:
    import aiosqlite


def job_row(
    id: str = "rj-000000000001",  # noqa: A002
    *,
    status: RelayStatus = RelayStatus.QUEUED,
    created_at: str = "2026-09-09T10:00:00+00:00",
    deliver: RelayDelivery | None = RelayDelivery(  # noqa: B008
        label="brain", exec="membrane resume"
    ),
) -> RelayJob:
    return RelayJob(
        id=id,
        account_id="acct-1",
        api_key_id="key-1",
        status=status,
        target="https://api.anthropic.com/v1/messages",
        method="POST",
        forward_headers={"x-api-key": "sk-secret"},
        body={"model": "claude-opus-5", "stream": True},
        retry=RetryPolicy(),
        timeout_seconds=3600,
        deliver=deliver,
        attempts=0,
        error=None,
        response_status=None,
        response_headers=None,
        response_body=None,
        delivery_status=None if deliver is None else DeliveryStatus.PENDING,
        delivery_attempts=0,
        delivery_computer_id=None,
        delivery_deferred_id=None,
        delivery_error=None,
        created_at=created_at,
        updated_at=created_at,
    )


async def test_insert_get_and_update_round_trip(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row())
    job = job_row()
    await insert_relay_job(db, job)
    assert await get_relay_job(db, job.id) == job
    assert await get_relay_job(db, "rj-nope") is None
    job.status = RelayStatus.COMPLETED
    job.attempts = 2
    job.forward_headers = None
    job.response_status = 200
    job.response_headers = {"content-type": "application/json"}
    job.response_body = {"content": [{"type": "text", "text": "hi"}]}
    job.delivery_status = DeliveryStatus.DELIVERED
    job.delivery_attempts = 1
    job.delivery_computer_id = "comp-9"
    job.updated_at = "2026-09-09T10:05:00+00:00"
    await update_relay_job(db, job)
    assert await get_relay_job(db, job.id) == job


async def test_a_string_body_and_no_delivery_survive(db: aiosqlite.Connection) -> None:
    job = job_row(deliver=None)
    job.body = "raw text"
    job.response_body = "not json"
    await insert_relay_job(db, job)
    back = await get_relay_job(db, job.id)
    assert back is not None
    assert back.body == "raw text" and back.response_body == "not json"
    assert back.deliver is None and back.delivery_status is None


async def test_list_by_status_and_expiry(db: aiosqlite.Connection) -> None:
    await insert_relay_job(db, job_row("rj-1", status=RelayStatus.QUEUED))
    await insert_relay_job(db, job_row("rj-2", status=RelayStatus.IN_PROGRESS))
    await insert_relay_job(
        db, job_row("rj-3", status=RelayStatus.COMPLETED, created_at="2026-09-01T00:00:00+00:00")
    )
    unsettled = await list_relay_jobs_by_status(db, (RelayStatus.QUEUED, RelayStatus.IN_PROGRESS))
    assert [j.id for j in unsettled] == ["rj-1", "rj-2"]
    assert await delete_relay_jobs_before(db, "2026-09-05T00:00:00+00:00") == 1
    assert await get_relay_job(db, "rj-3") is None
    assert await get_relay_job(db, "rj-1") is not None
