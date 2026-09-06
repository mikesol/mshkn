from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import asyncssh
import pytest

import mshkn.host.ssh as ssh_module
from mshkn.host import ExecResult
from mshkn.host.ssh import SshGuest


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


class FakeConn:
    def __init__(self, process: FakeProcess) -> None:
        self._process = process
        self.closed = False

    async def create_process(self, command: str) -> FakeProcess:
        return self._process

    async def run(self, command: str, check: bool = False) -> Any:
        class R:
            exit_status = 0
            stdout = "ok\n"
            stderr = ""

        return R()

    def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


def make_guest(process: FakeProcess) -> SshGuest:
    async def connect(host: str, **kwargs: Any) -> FakeConn:
        return FakeConn(process)

    return SshGuest(Path("/tmp/k"), connect=connect)


async def test_stream_yields_lines_before_the_process_exits() -> None:
    process = FakeProcess(stdout=[(0.0, "a\n"), (0.05, "b\n")], stderr=[], exit_after=0.3)
    guest = make_guest(process)
    seen: list[tuple[float, tuple[str, str]]] = []
    t0 = time.monotonic()
    async for item in guest.stream("172.16.1.2", "cmd"):
        seen.append((time.monotonic() - t0, item))
    names = [item for _, item in seen]
    assert names == [("stdout", "a"), ("stdout", "b"), ("exit", "0")]
    assert seen[0][0] < 0.2, "first line must arrive before process exit (0.3s)"
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


async def test_exec_uses_pooled_connection_and_parses_result() -> None:
    process = FakeProcess([], [], 0)
    guest = make_guest(process)
    result = await guest.exec("172.16.1.2", "true")
    assert result == ExecResult(exit_code=0, stdout="ok\n", stderr="")


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
