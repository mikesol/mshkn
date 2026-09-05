from __future__ import annotations

from pathlib import Path

from mshkn.config import Config
from mshkn.host.firecracker import (
    STAGING_DRIVE_NAME,
    STAGING_SLOT,
    STAGING_TAP,
    FirecrackerHypervisor,
)


class Recorder:
    def __init__(self, taps: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.taps = taps if taps is not None else set()

    async def __call__(self, cmd: str, check: bool = True) -> str:
        self.calls.append(cmd)
        if cmd.startswith("ip link show "):
            tap = cmd.split()[3]
            return f"7: {tap}: <UP>" if tap in self.taps else ""
        return ""


async def test_teardown_slot_skips_missing_tap() -> None:
    run = Recorder(taps=set())
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv.teardown_slot(5)
    assert not any(c.startswith("ip link del tap5") for c in run.calls)
    assert any(c.startswith("iptables -D FORWARD -i tap5") for c in run.calls)


async def test_teardown_slot_deletes_present_tap() -> None:
    run = Recorder(taps={"tap5"})
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv.teardown_slot(5)
    assert "ip link del tap5" in run.calls


async def test_staging_clean_is_quiet_when_nothing_to_clean() -> None:
    run = Recorder(taps=set())
    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")), run=run)
    await hv._ensure_staging_clean()
    assert not any(c == f"ip link del {STAGING_TAP}" for c in run.calls)
    assert f"dmsetup remove {STAGING_DRIVE_NAME}" in run.calls
    assert STAGING_SLOT == 254


def test_is_alive_for_own_pid_and_dead_pid() -> None:
    import os

    hv = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    assert hv.is_alive(os.getpid())
    assert not hv.is_alive(2**22 - 1)


def test_staging_lock_is_per_instance() -> None:
    a = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    b = FirecrackerHypervisor(Config(ssh_key_path=Path("/tmp/k")))
    assert a._staging_lock is not b._staging_lock
