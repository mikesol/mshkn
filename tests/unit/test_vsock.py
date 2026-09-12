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
        script = await reader.read()  # the host half-closes after the script
        seen.append(script)
        writer.write(b"out line\n" + f"__mshkn_rc={rc}\n".encode())
        await writer.drain()
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
    assert (rc, output) == (0, "out line\n")


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


async def test_vsock_run_fails_when_the_socket_never_appears(tmp_path: Path) -> None:
    with pytest.raises(HostError, match="vsock"):
        await vsock_run(tmp_path / "missing.sock", 52, "true", timeout=0.3)
