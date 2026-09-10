"""The relay's upstream half (#110): a job is accepted at once, called in the
background with lampas's retry policy, its headers deleted when the call settles,
its response stored whole, and re-run after a restart."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mshkn.config import Config
from mshkn.db import (
    claim_deferred_by_label,
    get_exec_log,
    get_relay_job,
    insert_account,
    insert_relay_job,
)
from mshkn.errors import InvalidInput, NotFound, PayloadTooLarge
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost
from mshkn.models import CheckpointTrigger, DeliveryStatus, RelayDelivery, RelayStatus, RetryPolicy
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService
from mshkn.services.relay import RelayRequest, RelayService, task_key
from tests.support import account_row
from tests.unit.test_relay_db import job_row

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = account_row(api_key="k")
Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response] | httpx.Response]


async def _public(hostname: str) -> list[str]:
    return ["93.184.216.34"]


class Relay:
    """A RelayService over a MockTransport, with the sleeps it took recorded."""

    def __init__(
        self, db: aiosqlite.Connection, tmp_path: Path, handler: Handler, **config: Any
    ) -> None:
        self.config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts", **config)
        host = FakeHost()
        self.host = host
        allocator = SlotAllocator()
        self.tasks = BackgroundTasks()
        recipes = RecipeService(
            self.config, db, host.blocks, host.hypervisor, allocator, self.tasks
        )
        computers = ComputerService(self.config, db, host, allocator, recipes)
        checkpoints = CheckpointService(self.config, db, host, allocator, computers, self.tasks)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
        lifecycle = Lifecycle(db, computers, checkpoints, self.tasks, self.http)
        self.slept: list[float] = []

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        self.service = RelayService(
            self.config,
            db,
            checkpoints,
            lifecycle,
            self.tasks,
            self.http,
            resolver=_public,
            sleep=sleep,
        )

    async def submit(self, **fields: Any) -> str:
        request = RelayRequest(
            target=fields.pop("target", "https://model.example/v1/messages"),
            method=fields.pop("method", "POST"),
            forward_headers=fields.pop("forward_headers", {"x-api-key": "sk-secret"}),
            body=fields.pop("body", {"model": "m", "stream": True}),
            retry=fields.pop("retry", RetryPolicy()),
            timeout_seconds=fields.pop("timeout_seconds", None),
            deliver=fields.pop("deliver", None),
        )
        assert not fields, fields
        job = await self.service.submit(ACCOUNT, request, api_key_id="key-1")
        assert job.status == RelayStatus.QUEUED and job.id.startswith("rj-")
        return job.id

    async def settled(self, job_id: str) -> Any:
        await self.tasks.wait(f"relay:{job_id}")
        job = await self.service.get_owned(ACCOUNT, job_id)
        assert job.forward_headers is None, "headers are deleted whatever the outcome"
        return job


@pytest.fixture
async def account(db: aiosqlite.Connection) -> None:
    await insert_account(db, ACCOUNT)


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


MESSAGE_STREAM = _sse(
    {
        "type": "message_start",
        "message": {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "m",
            "content": [],
            "usage": {"input_tokens": 3, "output_tokens": 1},
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    {"type": "message_stop"},
)


async def test_a_job_is_called_once_with_its_headers_and_body_and_the_stream_is_reassembled(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=MESSAGE_STREAM
        )

    relay = Relay(db, tmp_path, handler)
    job_id = await relay.submit()
    job = await relay.settled(job_id)
    assert job.status == RelayStatus.COMPLETED and job.attempts == 1 and job.error is None
    assert job.response_status == 200
    assert job.response_body["content"] == [{"type": "text", "text": "hi"}]
    assert job.response_body["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert job.response_headers["content-type"] == "text/event-stream"
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://model.example/v1/messages"
    assert request.headers["x-api-key"] == "sk-secret"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"model": "m", "stream": True}
    assert relay.slept == []


async def test_json_and_text_bodies_are_stored_as_they_are_and_get_sends_none(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/json":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<title>Example Domain</title>"
        )

    relay = Relay(db, tmp_path, handler)
    as_json = await relay.settled(
        await relay.submit(target="https://model.example/json", body="raw", forward_headers={})
    )
    assert as_json.response_body == {"ok": True}
    assert seen[0].content == b"raw" and "content-type" not in seen[0].headers
    as_text = await relay.settled(
        await relay.submit(target="https://model.example/page", method="GET", body=None)
    )
    assert as_text.response_body == "<title>Example Domain</title>"
    assert seen[1].method == "GET" and seen[1].content == b""


async def test_5xx_429_and_transport_errors_are_retried_with_the_policy_then_fail(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("down")
        if calls == 2:
            return httpx.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error"}}
            )
        return httpx.Response(429, json={})

    relay = Relay(db, tmp_path, handler)
    job = await relay.settled(
        await relay.submit(retry=RetryPolicy(attempts=3, initial_delay_ms=500, max_delay_ms=800))
    )
    assert job.status == RelayStatus.FAILED and job.attempts == 3
    assert job.error == "HTTP 429"
    assert job.response_status == 429, "the last response is kept beside the error"
    assert relay.slept == [0.5, 0.8]


async def test_a_retry_that_then_succeeds_completes(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, json={"n": calls})

    relay = Relay(db, tmp_path, handler)
    job = await relay.settled(await relay.submit())
    assert job.status == RelayStatus.COMPLETED and job.attempts == 2 and job.error is None
    assert job.response_body == {"n": 2} and relay.slept == [1.0]


async def test_an_overloaded_stream_error_is_retried_and_another_is_final(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Overloaded"},
                    }
                ),
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
            ),
        )

    relay = Relay(db, tmp_path, handler)
    job = await relay.settled(await relay.submit())
    assert job.status == RelayStatus.FAILED and job.attempts == 2
    assert "invalid_request_error" in (job.error or "") and relay.slept == [1.0]


async def test_a_4xx_is_completed_with_its_status_and_a_cut_stream_is_retried(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    streams = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal streams
        if request.url.path == "/bad":
            return httpx.Response(400, json={"error": {"message": "max_tokens too large"}})
        streams += 1
        if streams == 1:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=MESSAGE_STREAM[:80]
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=MESSAGE_STREAM
        )

    relay = Relay(db, tmp_path, handler)
    bad = await relay.settled(await relay.submit(target="https://model.example/bad"))
    assert bad.status == RelayStatus.COMPLETED and bad.response_status == 400 and bad.attempts == 1
    assert bad.response_body == {"error": {"message": "max_tokens too large"}}
    cut = await relay.settled(await relay.submit())
    assert cut.status == RelayStatus.COMPLETED and cut.attempts == 2 and relay.slept == [1.0]


async def test_a_timeout_and_an_oversized_response_are_final(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 2048)

    relay = Relay(db, tmp_path, handler, relay_body_bytes=1024)
    slow = await relay.settled(await relay.submit(target="https://model.example/slow"))
    assert (
        slow.status == RelayStatus.FAILED and slow.attempts == 1 and "timeout" in (slow.error or "")
    )
    big = await relay.settled(
        await relay.submit(target="https://model.example/big", method="GET", body=None)
    )
    assert big.status == RelayStatus.FAILED and big.attempts == 1 and "1024" in (big.error or "")
    assert relay.slept == []


async def test_submit_refuses_a_blocked_target_an_over_cap_timeout_and_a_big_body(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(
        db,
        tmp_path,
        lambda _: httpx.Response(200),
        relay_body_bytes=64,
        relay_timeout_seconds=10,
    )
    with pytest.raises(InvalidInput, match="blocked"):
        await relay.submit(target="http://127.0.0.1:8000/health")
    with pytest.raises(InvalidInput, match="timeout_seconds"):
        await relay.submit(timeout_seconds=11)
    with pytest.raises(PayloadTooLarge):
        await relay.submit(body={"pad": "x" * 100})
    job_id = await relay.submit(body={"ok": 1}, timeout_seconds=10)
    job = await relay.settled(job_id)
    assert job.timeout_seconds == 10
    default = await relay.settled(await relay.submit(body={"ok": 1}))
    assert default.timeout_seconds == 10


async def test_get_owned_is_scoped_to_the_account(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda _: httpx.Response(200, json={}))
    job_id = await relay.submit()
    await relay.settled(job_id)
    with pytest.raises(NotFound):
        await relay.service.get_owned(account_row("acct-2", api_key="o"), job_id)
    with pytest.raises(NotFound):
        await relay.service.get_owned(ACCOUNT, "rj-nope")


async def test_resume_re_runs_jobs_still_queued_or_in_progress(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    relay = Relay(db, tmp_path, handler)
    await insert_relay_job(db, job_row("rj-queued", status=RelayStatus.QUEUED, deliver=None))
    interrupted = job_row("rj-running", status=RelayStatus.IN_PROGRESS, deliver=None)
    interrupted.attempts = 1
    await insert_relay_job(db, interrupted)
    await insert_relay_job(db, job_row("rj-done", status=RelayStatus.COMPLETED, deliver=None))
    assert await relay.service.resume() == 2
    queued = await relay.settled("rj-queued")
    running = await relay.settled("rj-running")
    assert queued.status == RelayStatus.COMPLETED and queued.attempts == 1
    assert running.status == RelayStatus.COMPLETED and running.attempts == 2
    done = await get_relay_job(db, "rj-done")
    assert done is not None and done.forward_headers == {"x-api-key": "sk-secret"}, "untouched"


async def _chain(relay: Relay, db: aiosqlite.Connection, label: str) -> None:
    """A labelled head to fork: a computer checkpointed under `label` and destroyed."""
    computers = relay.service.lifecycle.computers
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await relay.service.checkpoints.create(base, label=label, trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)


async def test_a_settled_job_wakes_its_chain_with_the_job_id(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda _: httpx.Response(200, json={"answer": 42}))
    await _chain(relay, db, "brain")
    host_guest = relay.host.guest
    job_id = await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    host_guest.script[f"membrane resume {job_id}"] = ExecResult(0, "resumed\n", "")
    job = await relay.settled(job_id)
    assert job.status == RelayStatus.COMPLETED
    assert job.delivery_status == DeliveryStatus.DELIVERED and job.delivery_attempts == 1
    assert job.delivery_computer_id is not None and job.delivery_deferred_id is None
    log = await get_exec_log(db, job.delivery_computer_id)
    assert (
        log is not None and log.command == f"membrane resume {job_id}" and log.stdout == "resumed\n"
    )
    assert log.label == "brain" and log.created_checkpoint_id is not None
    heads = await relay.service.checkpoints.list(ACCOUNT, label="brain")
    assert len(heads) == 2 and heads[0].id == log.created_checkpoint_id


async def test_a_failed_job_is_delivered_too(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    relay = Relay(db, tmp_path, handler)
    await _chain(relay, db, "brain")
    job = await relay.settled(
        await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    )
    assert job.status == RelayStatus.FAILED and job.delivery_status == DeliveryStatus.DELIVERED
    assert job.delivery_computer_id is not None


async def test_a_busy_chain_defers_the_wake_up(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda _: httpx.Response(200, json={}))
    await _chain(relay, db, "brain")
    # a computer forked from the head is still running on the label
    head = await relay.service.checkpoints.latest_for_label(ACCOUNT, "brain")
    assert head is not None
    await relay.service.lifecycle.computers.fork(ACCOUNT, head, recipe_id=None, api_key_id=None)
    job = await relay.settled(
        await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    )
    assert job.delivery_status == DeliveryStatus.DELIVERED
    assert job.delivery_computer_id is None and job.delivery_deferred_id is not None
    queued = await claim_deferred_by_label(db, "brain")
    assert (
        len(queued) == 1
        and json.loads(queued[0].request_payload)["exec"] == f"membrane resume {job.id}"
    )


async def test_a_fork_that_raises_is_retried_then_the_delivery_fails(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    relay = Relay(db, tmp_path, lambda _: httpx.Response(200, json={}))
    # no chain: every fork is NotFound
    job = await relay.settled(
        await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    )
    assert job.status == RelayStatus.COMPLETED, "a failed delivery never loses the result"
    assert job.delivery_status == DeliveryStatus.FAILED and job.delivery_attempts == 3
    assert job.delivery_error is not None and "NotFound" in job.delivery_error
    assert relay.slept == [1.0, 2.0]


async def test_run_ephemeral_failing_after_a_good_fork_never_forks_again(
    db: aiosqlite.Connection, tmp_path: Path, account: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dm-thin pool filling after the fork is admitted must not turn into a
    second fork, and must not touch the job's own settled result."""
    relay = Relay(db, tmp_path, lambda _: httpx.Response(200, json={"answer": 42}))
    await _chain(relay, db, "brain")
    fork_calls = 0
    real_fork_by_label = relay.service.checkpoints.fork_by_label

    async def counting_fork_by_label(*args: Any, **kwargs: Any) -> Any:
        nonlocal fork_calls
        fork_calls += 1
        return await real_fork_by_label(*args, **kwargs)

    monkeypatch.setattr(relay.service.checkpoints, "fork_by_label", counting_fork_by_label)

    async def exploding_run_ephemeral(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("dm-thin pool full")

    monkeypatch.setattr(relay.service.lifecycle, "run_ephemeral", exploding_run_ephemeral)
    job = await relay.settled(
        await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    )
    assert job.status == RelayStatus.COMPLETED and job.response_body == {"answer": 42}
    assert job.delivery_status == DeliveryStatus.DELIVERED
    assert job.delivery_error is not None and "dm-thin pool full" in job.delivery_error
    assert job.delivery_deferred_id is None and job.delivery_computer_id is not None
    assert fork_calls == 1


async def test_an_unforeseen_error_fails_the_job_and_still_wakes_its_chain(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    """`reassemble` raises for a malformed event stream in ways `_attempt` does not
    convert. The task must not die with the job `in_progress`: the wake-up is the
    only thing that closes the turn, so an unforeseen failure still delivers."""
    broken = b'event: message_start\ndata: {"type": "message_start"}\n\n'
    relay = Relay(
        db,
        tmp_path,
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=broken
        ),
    )
    await _chain(relay, db, "brain")
    job_id = await relay.submit(deliver=RelayDelivery(label="brain", exec="membrane resume"))
    relay.host.guest.script[f"membrane resume {job_id}"] = ExecResult(0, "resumed\n", "")
    job = await relay.settled(job_id)
    assert job.status == RelayStatus.FAILED
    assert job.error is not None and "KeyError" in job.error
    assert job.delivery_status == DeliveryStatus.DELIVERED
    log = await get_exec_log(db, job.delivery_computer_id or "")
    assert log is not None and log.command == f"membrane resume {job_id}"


async def test_a_cancelled_call_keeps_its_headers_so_a_restart_can_re_run_it(
    db: aiosqlite.Connection, tmp_path: Path, account: None
) -> None:
    """`Runtime.close` cancels what its drain did not finish. A cancelled task is
    not a settled one: the row stays `in_progress` with its credentials, which is
    what `resume` needs to re-run the call."""
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-api-key", ""))
        started.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    relay = Relay(db, tmp_path, handler)
    job_id = await relay.submit()
    await started.wait()
    await relay.tasks.cancel(task_key(job_id))
    interrupted = await get_relay_job(db, job_id)
    assert interrupted is not None and interrupted.status == RelayStatus.IN_PROGRESS
    assert interrupted.forward_headers == {"x-api-key": "sk-secret"}
    release.set()
    assert await relay.service.resume() == 1
    job = await relay.settled(job_id)
    assert job.status == RelayStatus.COMPLETED and job.response_body == {"ok": True}
    assert seen == ["sk-secret", "sk-secret"], "the re-run carries the credentials"
