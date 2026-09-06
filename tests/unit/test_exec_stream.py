"""POST /computers/{id}/exec streams guest output as server-sent events.

The response headers go out before the first line is read, so a guest failure
mid-stream cannot become an HTTP error. The endpoint turns it into an `error`
event followed by `exit: 255` instead of raising through the response body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account, insert_computer
from mshkn.host.fake import FakeHost
from mshkn.observability.metrics import operation_duration_seconds, operation_errors_total
from tests.support import account_row, computer_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

AUTH = {"Authorization": "Bearer test-key"}


async def _running_computer(db: aiosqlite.Connection) -> None:
    await insert_account(db, account_row())
    await insert_computer(db, computer_row(1))


def _events(body: str) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs, in order."""
    events: list[tuple[str, str]] = []
    name: str | None = None
    for raw in body.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((name or "message", line[len("data:") :].strip()))
            name = None
    return events


async def test_exec_streams_stdout_then_exit(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    await _running_computer(db)
    host = FakeHost()
    host.guest.stream_script["ls /"] = [("stdout", "bin"), ("stdout", "etc")]
    app = make_app(make_runtime(db, config=runtime_config, host=host))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/computers/comp-1/exec",
            json={"command": "ls /"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    assert _events(resp.text) == [
        ("stdout", "bin"),
        ("stdout", "etc"),
        ("exit", "0"),
    ]
    assert host.guest.commands == [("172.16.1.2", "ls /")]


async def test_exec_stream_failure_becomes_error_and_exit_events(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """A guest failure after the response has started is reported in-band."""
    await _running_computer(db)
    host = FakeHost()
    host.guest.fail_next("stream")
    app = make_app(make_runtime(db, config=runtime_config, host=host))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/computers/comp-1/exec",
            json={"command": "ls /"},
            headers=AUTH,
        )

    # The headers were already sent, so the request itself still succeeded.
    assert resp.status_code == 200
    events = _events(resp.text)
    assert [name for name, _ in events] == ["error", "exit"]
    assert events[0][1].startswith("HostError:")
    assert events[1][1] == "255"


async def test_stream_is_timed_under_op_exec_and_counts_host_failures(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    await _running_computer(db)
    host = FakeHost()
    host.guest.stream_script["ls /"] = [("stdout", "bin")]
    app = make_app(make_runtime(db, config=runtime_config, host=host))
    before_ok = operation_duration_seconds.labels(op="exec")._sum.get()
    before_err = operation_errors_total.labels(op="exec", kind="host")._value.get()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/computers/comp-1/exec", json={"command": "ls /"}, headers=AUTH)
        host.guest.fail_next("stream")
        resp = await client.post("/computers/comp-1/exec", json={"command": "ls /"}, headers=AUTH)
    assert resp.status_code == 200 and _events(resp.text)[-1] == ("exit", "255")
    assert operation_duration_seconds.labels(op="exec")._sum.get() >= before_ok
    assert operation_errors_total.labels(op="exec", kind="host")._value.get() == before_err + 1
