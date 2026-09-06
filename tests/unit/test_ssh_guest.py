from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import asyncssh
import pytest

import mshkn.host.ssh as ssh_module
from mshkn.errors import HostError
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
        run_delay: float = 0.0,
    ) -> None:
        self._process = process
        self._channel_error = channel_error
        self._run_error = run_error
        self._run_delay = run_delay
        self.closed = False
        self.runs: list[str] = []

    async def create_process(self, command: str) -> Any:
        if self._channel_error:
            raise asyncssh.ChannelOpenError(4, "open failed")
        return self._process

    async def run(self, command: str, check: bool = False) -> Any:
        self.runs.append(command)
        if self._run_delay:
            await asyncio.sleep(self._run_delay)
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
    with pytest.raises(HostError) as info:
        async for _item in guest.stream("172.16.1.2", "cmd", timeout=0.5):
            pass
    assert isinstance(info.value.__cause__, asyncssh.ConnectionLost)
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
    await guest.warm("172.16.1.2")
    assert len(created) == 2, "close() must leave nothing pooled, so the next warm reconnects"


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
    with pytest.raises(HostError) as info:
        async for _item in guest.stream("172.16.1.2", "cmd"):
            pass
    assert isinstance(info.value.__cause__, asyncssh.ChannelOpenError)
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


async def test_concurrent_warms_share_one_connection() -> None:
    """The per-IP lock is held across the handshake, so a second warm waits for it."""
    created: list[FakeConn] = []

    async def connect(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(0.1)
        conn = FakeConn(FakeProcess([], [], 0))
        created.append(conn)
        return conn

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    await asyncio.gather(guest.warm("172.16.1.2"), guest.warm("172.16.1.2"))
    assert len(created) == 1, f"the per-IP lock must hold across connect, got {len(created)} conns"


async def test_evict_during_connect_discards_the_new_connection() -> None:
    """evict() takes no lock, so a connect for the same IP can still be in flight.

    That connection is to a VM being destroyed. Pooling it would hand the next
    VM on the recycled slot a connection to a dead machine.
    """
    created: list[FakeConn] = []

    async def connect(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(0.1)
        conn = FakeConn(FakeProcess([], [], 0))
        created.append(conn)
        return conn

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    pending = asyncio.create_task(guest.warm("172.16.1.2"))
    await asyncio.sleep(0.01)
    await guest.evict("172.16.1.2")
    with pytest.raises(HostError) as info:
        await pending
    assert isinstance(info.value.__cause__, asyncssh.ConnectionLost)
    assert created[0].closed, "the raced connection must be closed, not leaked"
    # Nothing was stored, so the next warm has to connect again.
    await guest.warm("172.16.1.2")
    assert len(created) == 2


async def test_exec_evicts_only_the_connection_that_failed() -> None:
    """A lost exec must not close the replacement another task already pooled.

    Otherwise one eviction cascades: every in-flight exec on the VM tears down
    the connection the next one just established.
    """
    lost = FakeConn(
        FakeProcess([], [], 0), run_error=asyncssh.ConnectionLost("gone"), run_delay=0.1
    )
    replacement = FakeConn(FakeProcess([], [], 0))
    spare = FakeConn(FakeProcess([], [], 0))
    guest = make_guest_with([lost, replacement, spare])
    running = asyncio.create_task(guest.exec("172.16.1.2", "slow"))
    await asyncio.sleep(0.01)
    await guest.evict("172.16.1.2")  # e.g. a checkpoint pause/resume
    await guest.warm("172.16.1.2")  # another task pools the replacement
    assert await running == ExecResult(exit_code=0, stdout="ok\n", stderr="")
    assert not replacement.closed, "the retry must reuse the pooled connection, not evict it"
    assert replacement.runs == ["slow"], "the retry must run on the pooled replacement"
    assert not spare.closed and spare.runs == [], "no third connection should have been needed"


async def test_health_check_tolerates_an_evict_during_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evict() can pop the connection a health probe is still testing."""
    monkeypatch.setattr(ssh_module, "_HEALTH_CHECK_INTERVAL", 0.0)
    stale = FakeConn(
        FakeProcess([], [], 0), run_error=asyncssh.ConnectionLost("gone"), run_delay=0.1
    )
    replacement = FakeConn(FakeProcess([], [], 0))
    guest = make_guest_with([stale, replacement])
    await guest.warm("172.16.1.2")
    probing = asyncio.create_task(guest.warm("172.16.1.2"))
    await asyncio.sleep(0.01)
    await guest.evict("172.16.1.2")
    await probing
    assert await guest.exec("172.16.1.2", "x") == ExecResult(exit_code=0, stdout="ok\n", stderr="")
    assert replacement.runs[-1] == "x"


async def test_a_healthy_probe_does_not_resurrect_an_evicted_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evict() during a *succeeding* probe must not hand back the closed connection."""
    monkeypatch.setattr(ssh_module, "_HEALTH_CHECK_INTERVAL", 0.0)
    stale = FakeConn(FakeProcess([], [], 0), run_delay=0.1)
    replacement = FakeConn(FakeProcess([], [], 0))
    guest = make_guest_with([stale, replacement])
    await guest.warm("172.16.1.2")
    racing = asyncio.create_task(guest.exec("172.16.1.2", "x"))
    await asyncio.sleep(0.01)
    await guest.evict("172.16.1.2")
    await racing
    assert stale.closed
    assert stale.runs == ["true"], "nothing but the probe may run on the evicted connection"
    assert replacement.runs[-1] == "x", "the evicted connection was handed back"


async def test_a_slow_handshake_does_not_make_the_next_call_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The health-check clock starts when the connection is usable, not when asked for.

    A handshake can eat a large share of the interval; timing it from the
    request would make the very next call probe a connection that just came up.
    """
    monkeypatch.setattr(ssh_module, "_HEALTH_CHECK_INTERVAL", 0.15)
    conn = FakeConn(FakeProcess([], [], 0))

    async def connect(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(0.2)
        return conn

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    await guest.warm("172.16.1.2")
    await guest.warm("172.16.1.2")
    assert conn.runs == [], "a connection that just finished its handshake must not be probed"


async def test_connect_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_module, "CONNECT_TIMEOUT_SECONDS", 0.05)

    async def hanging(host: str, **kwargs: Any) -> FakeConn:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    guest = SshGuest(Path("/tmp/k"), connect=hanging)
    start = time.monotonic()
    with pytest.raises(HostError) as info:
        await guest.warm("172.16.1.2")
    assert isinstance(info.value.__cause__, TimeoutError)
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
