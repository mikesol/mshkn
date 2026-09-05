"""Test SSH module imports and types.

SSH functions require a real VM, so we only test that the module imports
correctly and the dataclass is well-formed.
"""

import asyncio
import time
from pathlib import Path

import asyncssh
import pytest

import mshkn.vm.ssh as ssh_module
from mshkn.vm.ssh import ExecResult, _get_conn


def test_exec_result_fields() -> None:
    r = ExecResult(exit_code=0, stdout="hello\n", stderr="")
    assert r.exit_code == 0
    assert r.stdout == "hello\n"
    assert r.stderr == ""


def test_exec_result_nonzero() -> None:
    r = ExecResult(exit_code=1, stdout="", stderr="error\n")
    assert r.exit_code == 1
    assert r.stderr == "error\n"


def test_ssh_module_exports() -> None:
    from mshkn.vm.ssh import (
        ssh_download,
        ssh_exec,
        ssh_exec_bg,
        ssh_exec_stream,
        ssh_upload,
    )

    # Just verify they are callable
    assert callable(ssh_exec)
    assert callable(ssh_exec_stream)
    assert callable(ssh_exec_bg)
    assert callable(ssh_upload)
    assert callable(ssh_download)


async def test_get_conn_bounds_connect_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_module, "CONNECT_TIMEOUT_SECONDS", 0.05)

    async def hanging_connect(*a: object, **k: object) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncssh, "connect", hanging_connect)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await _get_conn("172.16.1.2", Path("/tmp/k"))
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
