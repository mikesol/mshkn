from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from mshkn.host.firecracker import (
    kill_firecracker_process,
    start_firecracker_process,
    wait_for_port,
)


def _fake_binary(tmp_path: Path, *, creates_socket: bool, exits_at_once: bool = False) -> str:
    """A stand-in firecracker that records its pid at <socket>.pid, then blocks.

    `exec -a "$0" sleep 30` replaces the shell rather than forking, so the pid
    the helper returns is the sleeping process itself and killing it leaves
    nothing behind. Keeping the script path as argv[0] lets a test find any
    survivor with `pgrep -f`.
    """
    script = tmp_path / "fake-firecracker"
    if exits_at_once:
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)
        return str(script)
    body = '#!/bin/bash\nshift\necho $$ > "$1.pid"\n'  # argv: --api-sock <path>
    if creates_socket:
        body += 'touch "$1"\n'
    body += 'exec -a "$0" sleep 30\n'
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


def _survivors(binary: str) -> list[str]:
    """Pids whose command line still mentions this test's fake binary."""
    found = subprocess.run(["pgrep", "-f", binary], capture_output=True, text=True, check=False)
    return found.stdout.split()


async def test_start_returns_a_live_pid_and_kill_reaps_it(tmp_path: Path) -> None:
    socket_path = str(tmp_path / "fc.socket")
    Path(socket_path).write_text("stale")  # a stale socket file is removed first
    binary = _fake_binary(tmp_path, creates_socket=True)
    pid = await start_firecracker_process(socket_path, binary=binary)
    assert Path(socket_path).read_text() == "", "the stale socket was unlinked, not reused"
    os.kill(pid, 0)  # alive
    await kill_firecracker_process(pid)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert _survivors(binary) == [], "the test leaves no process behind"


async def test_start_times_out_when_the_socket_never_appears(tmp_path: Path) -> None:
    socket_path = str(tmp_path / "never.socket")
    binary = _fake_binary(tmp_path, creates_socket=False)
    with pytest.raises(TimeoutError, match="not created"):
        await start_firecracker_process(socket_path, binary=binary, socket_timeout=0.2)
    pid = int(Path(f"{socket_path}.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the child is killed and reaped, never orphaned
    assert _survivors(binary) == [], "the test leaves no process behind"


async def test_start_times_out_as_a_timeout_when_the_child_died_first(tmp_path: Path) -> None:
    """A Firecracker that exits before binding the socket still raises TimeoutError.

    The watcher reaps it during the poll, so `proc.kill()` raises ProcessLookupError.
    Suppressing that is what keeps `_stage` and `build_template` mapping this to a
    HostError instead of leaking an OS error.
    """
    socket_path = str(tmp_path / "gone.socket")
    with pytest.raises(TimeoutError, match="not created"):
        await start_firecracker_process(
            socket_path,
            binary=_fake_binary(tmp_path, creates_socket=False, exits_at_once=True),
            socket_timeout=0.3,
        )


async def test_kill_of_a_dead_pid_is_a_no_op() -> None:
    proc = await asyncio.create_subprocess_exec("true")
    await proc.wait()
    await kill_firecracker_process(proc.pid)  # no exception


async def test_wait_for_port_returns_when_a_listener_answers() -> None:
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await wait_for_port("127.0.0.1", port, timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()


async def test_wait_for_port_times_out_on_a_closed_port() -> None:
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(TimeoutError, match="did not become reachable"):
        await wait_for_port("127.0.0.1", port, timeout=0.2)
