"""The relay (#110), lampas folded into mshkn: a job is one upstream HTTP call
plus at most one delivery. The call runs in a background task with lampas's
retry policy; the forwarded headers are deleted the moment it settles; the
response is stored whole; delivery is a fork by label (spec §3, §5)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from mshkn.db import (
    get_account_by_id,
    get_relay_job,
    insert_relay_job,
    list_relay_jobs_by_status,
    update_relay_job,
)
from mshkn.errors import InvalidInput, NotFound, PayloadTooLarge
from mshkn.models import DeliveryStatus, ExecSpec, RelayJob, RelayStatus
from mshkn.services.checkpoints import Deferred
from mshkn.services.sse import IncompleteStream, StreamError, reassemble
from mshkn.services.ssrf import guard, resolve_host

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account, RelayDelivery, RetryPolicy
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.checkpoints import CheckpointService
    from mshkn.services.lifecycle import Lifecycle
    from mshkn.services.ssrf import Resolver

logger = logging.getLogger(__name__)

# What the SDK itself retries, plus any 5xx (529 included).
RETRY_STATUSES = frozenset({408, 409, 429})
BODYLESS_METHODS = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class RelayRequest:
    """A validated `POST /relay` body; the router builds it, the service checks the rest."""

    target: str
    method: str
    forward_headers: dict[str, str]
    body: object
    retry: RetryPolicy
    timeout_seconds: int | None
    deliver: RelayDelivery | None


@dataclass(frozen=True)
class _Upstream:
    status: int
    headers: dict[str, str]
    body: object


class _TransientError(Exception):
    """An attempt failed in a way the policy retries."""


class _FinalError(Exception):
    """An attempt failed for good."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _encode(body: object) -> bytes:
    return body.encode() if isinstance(body, str) else json.dumps(body).encode()


def task_key(job_id: str) -> str:
    return f"relay:{job_id}"


class RelayService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        checkpoints: CheckpointService,
        lifecycle: Lifecycle,
        tasks: BackgroundTasks,
        http: httpx.AsyncClient,
        *,
        resolver: Resolver = resolve_host,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.db = db
        self.checkpoints = checkpoints
        self.lifecycle = lifecycle
        self.tasks = tasks
        self.http = http
        self.resolve = resolver
        self.sleep = sleep

    # -- submit and read -----------------------------------------------------

    async def submit(
        self, account: Account, request: RelayRequest, *, api_key_id: str | None
    ) -> RelayJob:
        reason = await guard(request.target, self.resolve)
        if reason is not None:
            raise InvalidInput(f"target refused: {reason}")
        cap = self.config.relay_timeout_seconds
        timeout = cap if request.timeout_seconds is None else request.timeout_seconds
        if timeout > cap:
            raise InvalidInput(f"timeout_seconds must be at most {cap}")
        limit = self.config.relay_body_bytes
        if request.body is not None and len(_encode(request.body)) > limit:
            raise PayloadTooLarge(f"body exceeds {limit} bytes")
        now = _now()
        job = RelayJob(
            id=f"rj-{uuid.uuid4().hex[:12]}",
            account_id=account.id,
            api_key_id=api_key_id,
            status=RelayStatus.QUEUED,
            target=request.target,
            method=request.method,
            forward_headers=dict(request.forward_headers),
            body=request.body,
            retry=request.retry,
            timeout_seconds=timeout,
            deliver=request.deliver,
            attempts=0,
            error=None,
            response_status=None,
            response_headers=None,
            response_body=None,
            delivery_status=None if request.deliver is None else DeliveryStatus.PENDING,
            delivery_attempts=0,
            delivery_computer_id=None,
            delivery_deferred_id=None,
            delivery_error=None,
            created_at=now,
            updated_at=now,
        )
        await insert_relay_job(self.db, job)
        self._spawn(job)
        return job

    async def get_owned(self, account: Account, job_id: str) -> RelayJob:
        job = await get_relay_job(self.db, job_id)
        if job is None or job.account_id != account.id:
            raise NotFound("Relay job not found")
        return job

    async def resume(self) -> int:
        """Re-run every job a previous process left unsettled: its headers are
        still stored and the caller has seen nothing (spec §3)."""
        jobs = await list_relay_jobs_by_status(
            self.db, (RelayStatus.QUEUED, RelayStatus.IN_PROGRESS)
        )
        for job in jobs:
            self._spawn(job)
        return len(jobs)

    def _spawn(self, job: RelayJob) -> None:
        self.tasks.spawn(self.run(job), name=task_key(job.id), key=task_key(job.id))

    async def _save(self, job: RelayJob) -> None:
        job.updated_at = _now()
        await update_relay_job(self.db, job)

    # -- the upstream call ---------------------------------------------------

    async def run(self, job: RelayJob) -> None:
        try:
            await self._call(job)
        finally:
            # Deleted whatever happened, even a cancellation on shutdown.
            job.forward_headers = None
            await self._save(job)
        if job.deliver is not None:
            await self.deliver(job)

    async def _call(self, job: RelayJob) -> None:
        policy = job.retry
        for attempt in range(job.attempts, policy.attempts):
            job.attempts = attempt + 1
            job.status = RelayStatus.IN_PROGRESS
            await self._save(job)
            try:
                upstream = await self._attempt(job)
            except _FinalError as exc:
                job.status, job.error = RelayStatus.FAILED, str(exc)
                return
            except _TransientError as exc:
                job.error = str(exc)
                logger.warning("relay %s attempt %d failed: %s", job.id, job.attempts, exc)
                if attempt + 1 < policy.attempts:
                    await self.sleep(policy.delay(attempt))
                    continue
                job.status = RelayStatus.FAILED
                return
            job.response_status = upstream.status
            job.response_headers = upstream.headers
            job.response_body = upstream.body
            if upstream.status in RETRY_STATUSES or upstream.status >= 500:
                job.error = f"HTTP {upstream.status}"
                logger.warning("relay %s attempt %d: %s", job.id, job.attempts, job.error)
                if attempt + 1 < policy.attempts:
                    await self.sleep(policy.delay(attempt))
                    continue
                job.status = RelayStatus.FAILED
                return
            job.status, job.error = RelayStatus.COMPLETED, None
            return
        # A restart found the job with its attempts already spent.
        job.status = RelayStatus.FAILED
        job.error = job.error or "attempts exhausted"

    async def _attempt(self, job: RelayJob) -> _Upstream:
        headers = dict(job.forward_headers or {})
        content: bytes | None = None
        if job.method not in BODYLESS_METHODS and job.body is not None:
            content = _encode(job.body)
            if not isinstance(job.body, str) and not any(
                k.lower() == "content-type" for k in headers
            ):
                headers["content-type"] = "application/json"
        limit = self.config.relay_body_bytes
        seconds = float(job.timeout_seconds)
        try:
            async with (
                asyncio.timeout(seconds),
                self.http.stream(
                    job.method,
                    job.target,
                    headers=headers,
                    content=content,
                    timeout=httpx.Timeout(seconds),
                ) as response,
            ):
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise _FinalError(f"upstream body exceeds {limit} bytes")
                    chunks.append(chunk)
        except TimeoutError as exc:
            raise _FinalError(f"timeout after {job.timeout_seconds} s") from exc
        except httpx.TimeoutException as exc:
            raise _FinalError(
                f"timeout after {job.timeout_seconds} s: {type(exc).__name__}"
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
            raise _TransientError(f"{type(exc).__name__}: {exc}") from exc
        text = b"".join(chunks).decode(errors="replace")
        content_type = response.headers.get("content-type", "")
        body: object
        if "text/event-stream" in content_type:
            try:
                body = reassemble(text)
            except StreamError as exc:
                if exc.retryable:
                    raise _TransientError(str(exc)) from exc
                raise _FinalError(str(exc)) from exc
            except IncompleteStream as exc:
                raise _TransientError(str(exc)) from exc
        elif "json" in content_type:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
        else:
            body = text
        return _Upstream(response.status_code, dict(response.headers.items()), body)

    # -- delivery (Task 6) ---------------------------------------------------

    async def deliver(self, job: RelayJob) -> None:
        """Wake the target chain: fork the label with the pinned exec plus the job
        id, self-destructing, deferred behind a running fork (spec §5). A fork that
        raises is retried with the job's policy; after that the delivery is failed
        and the job keeps its result: the brain's next command settles it itself."""
        assert job.deliver is not None
        account = await get_account_by_id(self.db, job.account_id)
        if account is None:
            job.delivery_status, job.delivery_error = DeliveryStatus.FAILED, "account not found"
            await self._save(job)
            return
        spec = ExecSpec(
            command=f"{job.deliver.exec} {job.id}",
            self_destruct=True,
            callback_url=None,
            label=None,
            meta_exec=None,
        )
        policy = job.retry
        for attempt in range(policy.attempts):
            job.delivery_attempts = attempt + 1
            try:
                head, forked = await self.checkpoints.fork_by_label(
                    account, job.deliver.label, spec, exclusive="defer_on_conflict", recipe_id=None
                )
                if isinstance(forked, Deferred):
                    job.delivery_deferred_id = forked.deferred_id
                else:
                    job.delivery_computer_id = forked.id
                job.delivery_status, job.delivery_error = DeliveryStatus.DELIVERED, None
                await self._save(job)
                if not isinstance(forked, Deferred):
                    await self.lifecycle.run_ephemeral(
                        account, forked, spec, source_checkpoint=head
                    )
                return
            except Exception as exc:
                job.delivery_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "relay %s delivery attempt %d failed: %s", job.id, job.delivery_attempts, exc
                )
                await self._save(job)
                if attempt + 1 < policy.attempts:
                    await self.sleep(policy.delay(attempt))
        job.delivery_status = DeliveryStatus.FAILED
        await self._save(job)
