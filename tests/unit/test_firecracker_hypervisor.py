from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mshkn.config import Config
from mshkn.host.firecracker import (
    STAGING_DRIVE_NAME,
    STAGING_TAP,
    FirecrackerHypervisor,
)
from tests.support import ShellRecorder


async def test_teardown_slot_skips_missing_tap() -> None:
    run = ShellRecorder()
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv.teardown_slot(5)
    assert not any(c.startswith("ip link del tap5") for c, _ in run.calls)
    assert any(c.startswith("iptables -D FORWARD -i tap5") for c, _ in run.calls)


async def test_teardown_slot_deletes_present_tap() -> None:
    run = ShellRecorder(taps={"tap5"})
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv.teardown_slot(5)
    assert "ip link del tap5" in [c for c, _ in run.calls]


async def test_staging_clean_is_quiet_when_nothing_to_clean() -> None:
    run = ShellRecorder()
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv._ensure_staging_clean()
    assert not any(c == f"ip link del {STAGING_TAP}" for c, _ in run.calls)
    assert f"dmsetup remove {STAGING_DRIVE_NAME}" in [c for c, _ in run.calls]


def test_is_alive_for_own_pid_and_reaped_child() -> None:
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    assert hv.is_alive(os.getpid())
    child = subprocess.Popen(["true"])
    child.wait()
    assert not hv.is_alive(child.pid)


def test_staging_lock_is_per_instance() -> None:
    a = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    b = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    assert a._staging_lock is not b._staging_lock
