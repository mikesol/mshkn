"""POST /computers/{id}/exec streams guest output as server-sent events.

The response headers go out before the first line is read, so a guest failure
mid-stream cannot become an HTTP error. The endpoint turns it into an `error`
event followed by `exit: 255` instead of raising through the response body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.db import insert_account
from mshkn.host.fake import FakeHost
from mshkn.models import Account
from mshkn.resources import DEFAULT_RESOURCES
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.host.fake import FakeHostInstance
    from mshkn.models import Computer
    from mshkn.runtime import Runtime

AUTH = {"Authorization": "Bearer test-key"}

ACCOUNT = Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-03-08T00:00:00")


async def _running_computer(
    db: aiosqlite.Connection, config: Config, host: FakeHostInstance
) -> tuple[Runtime, Computer]:
    await insert_account(db, ACCOUNT)
    rt = make_runtime(db, config=config, host=host)
    computer = await rt.computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    return rt, computer


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
    host = FakeHost()
    host.guest.stream_script["ls /"] = [("stdout", "bin"), ("stdout", "etc")]
    rt, computer = await _running_computer(db, runtime_config, host)

    app = make_app(rt)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/computers/{computer.id}/exec",
            json={"command": "ls /"},
            headers=AUTH,
        )

    assert resp.status_code == 200
    assert _events(resp.text) == [
        ("stdout", "bin"),
        ("stdout", "etc"),
        ("exit", "0"),
    ]
    assert host.guest.commands == [(computer.vm_ip, "ls /")]


async def test_exec_stream_failure_becomes_error_and_exit_events(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """A guest failure after the response has started is reported in-band."""
    host = FakeHost()
    rt, computer = await _running_computer(db, runtime_config, host)
    host.guest.fail_next("stream")

    app = make_app(rt)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/computers/{computer.id}/exec",
            json={"command": "ls /"},
            headers=AUTH,
        )

    # The headers were already sent, so the request itself still succeeded.
    assert resp.status_code == 200
    events = _events(resp.text)
    assert [name for name, _ in events] == ["error", "exit"]
    assert events[0][1].startswith("HostError:")
    assert events[1][1] == "255"
