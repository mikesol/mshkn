from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from mshkn.host.firecracker import (
    kill_firecracker_process,
    start_firecracker_process,
    wait_for_port,
)


def _fake_binary(tmp_path: Path, *, creates_socket: bool) -> str:
    script = tmp_path / "fake-firecracker"
    body = "#!/bin/bash\n"
    if creates_socket:
        body += 'shift; touch "$1"\n'  # argv: --api-sock <path>
    body += "sleep 30\n"
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


async def test_start_returns_a_live_pid_and_kill_reaps_it(tmp_path: Path) -> None:
    socket_path = str(tmp_path / "fc.socket")
    Path(socket_path).write_text("stale")  # a stale socket file is removed first
    pid = await start_firecracker_process(
        socket_path, binary=_fake_binary(tmp_path, creates_socket=True)
    )
    assert Path(socket_path).exists()
    os.kill(pid, 0)  # alive
    await kill_firecracker_process(pid)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_start_times_out_when_the_socket_never_appears(tmp_path: Path) -> None:
    socket_path = str(tmp_path / "never.socket")
    with pytest.raises(TimeoutError, match="not created"):
        await start_firecracker_process(
            socket_path, binary=_fake_binary(tmp_path, creates_socket=False), socket_timeout=0.2
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
