"""vsock_run: a script into the guest over Firecracker's vsock Unix socket (#55)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mshkn.errors import HostError
from mshkn.host.firecracker import vsock_run

if TYPE_CHECKING:
    from pathlib import Path


async def _fake_firecracker(
    path: Path, *, listening: bool = True, rc: int = 0
) -> tuple[asyncio.AbstractServer, list[bytes]]:
    """A Unix socket that speaks Firecracker's host-initiated vsock handshake
    and then behaves like `socat VSOCK-LISTEN:52 EXEC:/bin/sh` on the guest side."""
    seen: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connect = await reader.readline()
        seen.append(connect)
        if not listening:
            writer.close()  # no guest listener: Firecracker drops the connection, no OK
            return
        writer.write(b"OK 1024\n")
        # Firecracker turns a host half-close into a full close, so the shell
        # reads lines until the script's own `exit`; nothing arrives as EOF.
        script = b""
        while True:
            line = await reader.readline()
            script += line
            if not line or line.strip() == b"exit":
                break
        seen.append(script)
        writer.write(b"out line\n" + f"__mshkn_rc={rc}\n".encode())
        await writer.drain()
        # socat lingers half a second after the shell exits before closing; the
        # host must not wait for that EOF.
        await asyncio.sleep(0.5)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path))
    return server, seen


async def test_vsock_run_connects_to_the_port_sends_the_script_and_reads_the_status(
    tmp_path: Path,
) -> None:
    server, seen = await _fake_firecracker(tmp_path / "v.sock")
    try:
        rc, output = await vsock_run(tmp_path / "v.sock", 52, "ip addr add x", timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()
    assert seen[0] == b"CONNECT 52\n"
    assert seen[1].startswith(b"ip addr add x\n")
    assert seen[1].rstrip().endswith(b"exit"), "the script ends the shell itself"
    assert (rc, output) == (0, "out line\n")


async def test_vsock_run_returns_at_the_status_line_without_waiting_for_eof(
    tmp_path: Path,
) -> None:
    import time

    server, _ = await _fake_firecracker(tmp_path / "v.sock")
    try:
        start = time.perf_counter()
        await vsock_run(tmp_path / "v.sock", 52, "true", timeout=2.0)
        elapsed = time.perf_counter() - start
    finally:
        server.close()
        await server.wait_closed()
    assert elapsed < 0.3, f"waited {elapsed:.2f}s for socat's linger instead of returning"


async def test_vsock_run_gives_up_at_once_when_the_socket_is_missing(tmp_path: Path) -> None:
    """Firecracker binds the socket inside load_snapshot; one that is not there
    right after belongs to a checkpoint without the device and never appears."""
    import time

    start = time.perf_counter()
    with pytest.raises(HostError, match="vsock"):
        await vsock_run(tmp_path / "missing.sock", 52, "true", timeout=2.0, connect_timeout=0.1)
    assert time.perf_counter() - start < 0.5


async def test_vsock_run_reports_the_scripts_exit_status(tmp_path: Path) -> None:
    server, _ = await _fake_firecracker(tmp_path / "v.sock", rc=3)
    try:
        rc, _out = await vsock_run(tmp_path / "v.sock", 52, "false", timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()
    assert rc == 3


async def test_vsock_run_fails_when_no_guest_listener_answers(tmp_path: Path) -> None:
    """A checkpoint taken before the guest had the listener: Firecracker closes
    the connection without an OK, and the caller falls back to SSH."""
    server, _ = await _fake_firecracker(tmp_path / "v.sock", listening=False)
    try:
        with pytest.raises(HostError, match="no OK"):
            await vsock_run(tmp_path / "v.sock", 52, "true", timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()
