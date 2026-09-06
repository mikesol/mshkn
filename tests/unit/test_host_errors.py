from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncssh
import pytest

from mshkn.config import Config
from mshkn.errors import HostError
from mshkn.host.firecracker import FirecrackerHypervisor
from mshkn.host.ssh import SshGuest


async def test_snapshot_on_a_missing_socket_is_a_host_error(tmp_path: Path) -> None:
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    with pytest.raises(HostError) as info:
        await hv.snapshot(str(tmp_path / "no-such.socket"), tmp_path / "snap")
    assert info.value.__cause__ is not None


async def test_ssh_connect_failure_is_a_host_error() -> None:
    async def connect(host: str, **kwargs: Any) -> Any:
        raise asyncssh.PermissionDenied("nope")

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    with pytest.raises(HostError) as info:
        await guest.exec("172.16.1.2", "true")
    assert isinstance(info.value.__cause__, asyncssh.PermissionDenied)


async def test_ssh_os_error_is_a_host_error() -> None:
    async def connect(host: str, **kwargs: Any) -> Any:
        raise OSError(113, "No route to host")

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    with pytest.raises(HostError):
        await guest.warm("172.16.1.2")


async def test_ssh_stream_connect_failure_is_a_host_error() -> None:
    async def connect(host: str, **kwargs: Any) -> Any:
        raise asyncssh.ConnectionLost("gone")

    guest = SshGuest(Path("/tmp/k"), connect=connect)
    with pytest.raises(HostError):
        async for _ in guest.stream("172.16.1.2", "true"):
            pass
