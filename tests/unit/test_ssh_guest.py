from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import asyncssh
import pytest

import mshkn.host.ssh as ssh_module
from mshkn.host import ExecResult
from mshkn.host.ssh import SshGuest, parse_metrics


class FakeReader:
    """Yields (delay, line) pairs; with hang_after=True it then blocks like an fd held open."""

    def __init__(self, lines: list[tuple[float, str]], *, hang_after: bool = False) -> None:
        self._lines = lines
        self._hang_after = hang_after

    def __aiter__(self) -> FakeReader:
        return self

    async def __anext__(self) -> str:
        if not self._lines:
            if self._hang_after:
                await asyncio.sleep(3600)
            raise StopAsyncIteration
        delay, line = self._lines.pop(0)
        await asyncio.sleep(delay)
        return line


class RaisingReader:
    """Yields its lines, then fails the way a dropped connection does."""

    def __init__(self, lines: list[str], error: Exception, *, delay: float = 0.0) -> None:
        self._lines = list(lines)
        self._error = error
        self._delay = delay

    def __aiter__(self) -> RaisingReader:
        return self

    async def __anext__(self) -> str:
        if not self._lines:
            await asyncio.sleep(self._delay)
            raise self._error
        return self._lines.pop(0)


class FakeProcess:
    def __init__(
        self,
        stdout: list[tuple[float, str]],
        stderr: list[tuple[float, str]],
        exit_after: float,
        code: int = 0,
        *,
        hang: bool = False,
    ) -> None:
        self.stdout = FakeReader(stdout, hang_after=hang)
        self.stderr = FakeReader(stderr, hang_after=hang)
        self._exit_after = exit_after
        self.exit_status = code
        self.killed = False

    async def wait(self) -> None:
        await asyncio.sleep(self._exit_after)

    def kill(self) -> None:
        self.killed = True


class LateStatusProcess:
    """Models asyncssh: the readers hit EOF before the exit-status request lands.

    ``exit_status`` is None until ``wait()`` resolves, exactly as
    ``SSHClientProcess.exit_status`` is until the channel delivers it.
    """

    def __init__(self, stdout: list[tuple[float, str]], *, exit_after: float, code: int) -> None:
        self.stdout = FakeReader(stdout)
        self.stderr = FakeReader([])
        self._exit_after = exit_after
        self._code = code
        self.exit_status: int | None = None
        self.killed = False

    async def wait(self) -> None:
        await asyncio.sleep(self._exit_after)
        self.exit_status = self._code

    def kill(self) -> None:
        self.killed = True


class LostConnectionProcess:
    """stdout fails mid-command; stderr and the process itself never finish."""

    def __init__(self, error: Exception) -> None:
        self.stdout = RaisingReader(["a\n"], error)
        self.stderr = FakeReader([], hang_after=True)
        self.exit_status: int | None = None
        self.killed = False

    async def wait(self) -> None:
        await asyncio.sleep(3600)

    def kill(self) -> None:
        self.killed = True


class DropAfterExitProcess:
    """The command exits cleanly; the connection then drops during the grace drain."""

    def __init__(self, error: Exception) -> None:
        self.stdout = RaisingReader(["a\n"], error, delay=0.05)
        self.stderr = FakeReader([])
        self.exit_status = 0
        self.killed = False

    async def wait(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


class RunResult:
    exit_status = 0
    stdout = "ok\n"
    stderr = ""


class FakeConn:
    def __init__(
        self,
        process: Any,
        *,
        channel_error: bool = False,
        run_error: Exception | None = None,
    ) -> None:
        self._process = process
        self._channel_error = channel_error
        self._run_error = run_error
        self.closed = False

    async def create_process(self, command: str) -> Any:
        if self._channel_error:
            raise asyncssh.ChannelOpenError(4, "open failed")
        return self._process

    async def run(self, command: str, check: bool = False) -> Any:
        if self._run_error is not None:
            raise self._run_error
        return RunResult()

    def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


def make_guest(process: Any) -> SshGuest:
    async def connect(host: str, **kwargs: Any) -> FakeConn:
        return FakeConn(process)

    return SshGuest(Path("/tmp/k"), connect=connect)


def make_guest_with(conns: list[FakeConn]) -> SshGuest:
    pending = list(conns)

    async def connect(host: str, **kwargs: Any) -> FakeConn:
        return pending.pop(0)

    return SshGuest(Path("/tmp/k"), connect=connect)


# -- streaming ---------------------------------------------------------------


async def test_stream_yields_lines_before_the_process_exits() -> None:
    process = FakeProcess(stdout=[(0.0, "a\n"), (0.05, "b\n")], stderr=[], exit_after=0.3)
    guest = make_guest(process)
    seen: list[tuple[float, tuple[str, str]]] = []
    t0 = time.monotonic()
    async for item in guest.stream("172.16.1.2", "cmd"):
        seen.append((time.monotonic() - t0, item))
    names = [item for _, item in seen]
    assert names == [("stdout", "a"), ("stdout", "b"), ("exit", "0")]
    # Tight enough that buffering until EOF (0.05s) fails, not just buffering
    # until the process exits (0.3s).
    assert seen[0][0] < 0.04, "line 'a' must arrive before line 'b' is even produced"
    assert seen[1][0] < 0.25


async def test_stream_kills_on_timeout_and_still_reports_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh_module, "STREAM_GRACE_SECONDS", 0.1)
    # readers stay open (a background child holds the fds) and the process never exits
    process = FakeProcess(stdout=[(0.0, "x\n")], stderr=[], exit_after=10, code=0, hang=True)
    guest = make_guest(process)
    items = [item async for item in guest.stream("172.16.1.2", "cmd", timeout=0.1)]
    assert process.killed
    assert items == [("stdout", "x"), ("exit", "0")]


async def test_stream_grace_drains_lines_after_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_module, "STREAM_GRACE_SECONDS", 0.2)
    process = FakeProcess(stdout=[(0.05, "late\n")], stderr=[(0.0, "err\n")], exit_after=0.0)
    guest = make_guest(process)
    items = [item async for item in guest.stream("172.16.1.2", "cmd")]
    assert ("stdout", "late") in items
    assert ("stderr", "err") in items
    assert items[-1] == ("exit", "0")


async def test_exit_event_waits_for_the_exit_status_after_readers_hit_eof() -> None:
    """EOF on both readers is not a stopping condition; the exit status is."""
    process = LateStatusProcess([(0.0, "a\n")], exit_after=0.1, code=7)
    guest = make_guest(process)
    items = [item async for item in guest.stream("172.16.1.2", "cmd")]
    assert items == [("stdout", "a"), ("exit", "7")]
    assert not process.killed


async def test_reader_failure_propagates_instead_of_a_clean_exit() -> None:
    process = LostConnectionProcess(asyncssh.ConnectionLost("boom"))
    guest = make_guest(process)
    with pytest.raises(asyncssh.ConnectionLost):
        async for _item in guest.stream("172.16.1.2", "cmd", timeout=0.5):
            pass
    assert process.killed, "a failed reader must still release the channel"


async def test_reader_failure_after_exit_reports_the_known_status() -> None:
    process = DropAfterExitProcess(asyncssh.ConnectionLost("boom"))
    guest = make_guest(process)
    seen = [item async for item in guest.stream("172.16.1.2", "cmd", timeout=0.5)]
    assert seen == [("stdout", "a"), ("exit", "0")]


async def test_close_waits_for_an_in_flight_connect() -> None:
    created: list[FakeConn] = []

    async def connect(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(0.1)
        conn = FakeConn(FakeProcess(stdout=[], stderr=[], exit_after=0))
        created.append(conn)
        return conn

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    warm = asyncio.create_task(guest.warm("172.16.1.2"))
    await asyncio.sleep(0.01)
    await guest.close()
    await warm
    assert len(created) == 1
    assert created[0].closed, "close() must not let a mid-handshake connection survive"
    assert guest._conns == {}


async def test_abandoning_the_stream_kills_the_process() -> None:
    process = FakeProcess(
        stdout=[(0.0, "a\n"), (0.0, "b\n"), (0.0, "c\n")], stderr=[], exit_after=10, hang=True
    )
    guest = make_guest(process)
    stream = guest.stream("172.16.1.2", "cmd", timeout=30.0)
    seen: list[tuple[str, str]] = []
    async for item in stream:
        seen.append(item)
        if len(seen) == 2:
            break
    await stream.aclose()
    assert seen == [("stdout", "a"), ("stdout", "b")]
    assert process.killed, "abandoning the stream must release the pooled connection's channel"


async def test_stream_falls_back_to_a_dedicated_connection_on_channel_limit() -> None:
    process = FakeProcess(stdout=[(0.0, "a\n")], stderr=[], exit_after=0.0)
    pooled = FakeConn(process, channel_error=True)
    dedicated = FakeConn(process)
    guest = make_guest_with([pooled, dedicated])
    items = [item async for item in guest.stream("172.16.1.2", "cmd")]
    assert items == [("stdout", "a"), ("exit", "0")]
    assert dedicated.closed, "the dedicated fallback must be closed once the stream ends"
    assert not pooled.closed, "the shared pooled connection must survive MaxSessions"


async def test_stream_closes_the_fallback_connection_when_the_retry_also_fails() -> None:
    pooled = FakeConn(FakeProcess([], [], 0), channel_error=True)
    dedicated = FakeConn(FakeProcess([], [], 0), channel_error=True)
    guest = make_guest_with([pooled, dedicated])
    with pytest.raises(asyncssh.ChannelOpenError):
        async for _item in guest.stream("172.16.1.2", "cmd"):
            pass
    assert dedicated.closed, "the dedicated fallback must not leak when its channel also fails"
    assert not pooled.closed


# -- exec and the pool -------------------------------------------------------


async def test_exec_uses_pooled_connection_and_parses_result() -> None:
    process = FakeProcess([], [], 0)
    guest = make_guest(process)
    result = await guest.exec("172.16.1.2", "true")
    assert result == ExecResult(exit_code=0, stdout="ok\n", stderr="")


async def test_exec_falls_back_to_a_dedicated_connection_on_channel_limit() -> None:
    pooled = FakeConn(FakeProcess([], [], 0), run_error=asyncssh.ChannelOpenError(4, "open failed"))
    dedicated = FakeConn(FakeProcess([], [], 0))
    guest = make_guest_with([pooled, dedicated])
    result = await guest.exec("172.16.1.2", "true")
    assert result == ExecResult(exit_code=0, stdout="ok\n", stderr="")
    assert dedicated.closed, "the dedicated fallback must be closed after the command"
    assert not pooled.closed, "MaxSessions must not tear down other tasks' channels"


async def test_exec_evicts_and_retries_when_the_connection_is_lost() -> None:
    pooled = FakeConn(FakeProcess([], [], 0), run_error=asyncssh.ConnectionLost("gone"))
    replacement = FakeConn(FakeProcess([], [], 0))
    guest = make_guest_with([pooled, replacement])
    result = await guest.exec("172.16.1.2", "true")
    assert result == ExecResult(exit_code=0, stdout="ok\n", stderr="")
    assert pooled.closed, "a lost connection must be evicted"
    assert not replacement.closed, "the replacement must stay in the pool"


async def test_evict_during_connect_does_not_create_a_second_connection() -> None:
    created: list[FakeConn] = []

    async def connect(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(0.1)
        conn = FakeConn(FakeProcess([], [], 0))
        created.append(conn)
        return conn

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    first = asyncio.create_task(guest.warm("172.16.1.2"))
    await asyncio.sleep(0.01)
    await guest.evict("172.16.1.2")
    second = asyncio.create_task(guest.warm("172.16.1.2"))
    await asyncio.gather(first, second)
    assert len(created) == 1, f"the per-IP lock must hold across connect, got {len(created)} conns"
    assert not created[0].closed


async def test_last_used_is_recorded_after_the_handshake() -> None:
    async def connect(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(0.1)
        return FakeConn(FakeProcess([], [], 0))

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    before = asyncio.get_running_loop().time()
    await guest.warm("172.16.1.2")
    assert guest._last_used["172.16.1.2"] >= before + 0.1


async def test_connect_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_module, "CONNECT_TIMEOUT_SECONDS", 0.05)

    async def hanging(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    guest = SshGuest(Path("/tmp/k"), connect=hanging)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await guest.warm("172.16.1.2")
    assert time.monotonic() - start < 1.0
    assert asyncssh is not None


# -- metrics -----------------------------------------------------------------


def test_parse_metrics_keeps_the_first_field_when_the_second_is_malformed() -> None:
    metrics = parse_metrics("5.0\n2048 notanumber\n10240 512\n1 init\n")
    assert metrics.cpu_pct == 95.0
    assert metrics.ram_total_mb == 2048
    assert metrics.ram_usage_mb == 0
    assert metrics.disk_total_mb == 10240
    assert metrics.disk_usage_mb == 512
    assert metrics.processes == [{"pid": 1, "command": "init"}]
