from __future__ import annotations

from pathlib import Path

from mshkn.host import (
    ExecResult,
    Host,
    PoolUsage,
    RunningVM,
    SnapshotFiles,
    VmMetrics,
)
from mshkn.host.network import slot_to_ip, tap_exists


def test_result_types_are_frozen_and_hashable() -> None:
    vm = RunningVM(pid=1, socket_path="/tmp/s", slot=3, vm_ip="172.16.3.2", tap_device="tap3")
    assert hash(vm)
    assert SnapshotFiles(vmstate=Path("/a"), memory=Path("/b")).memory == Path("/b")
    assert ExecResult(exit_code=0, stdout="x", stderr="").exit_code == 0
    metrics = VmMetrics(
        cpu_pct=1.0, ram_usage_mb=1, ram_total_mb=2, disk_usage_mb=3, disk_total_mb=4
    )
    assert metrics.processes == []
    assert PoolUsage(data_used_ratio=0.5, metadata_used_ratio=0.1).data_used_ratio == 0.5


def test_host_is_a_plain_container() -> None:
    assert set(Host.__dataclass_fields__) == {"hypervisor", "blocks", "guest", "objects", "proxy"}


async def test_tap_exists_uses_injected_run() -> None:
    calls: list[str] = []

    async def fake_run(cmd: str, check: bool = True) -> str:
        calls.append(cmd)
        return "5: tap5: <BROADCAST> mtu 1500" if "tap5" in cmd else ""

    assert await tap_exists("tap5", run=fake_run)
    assert not await tap_exists("tap9", run=fake_run)
    assert all(c.startswith("ip link show ") for c in calls)
    assert slot_to_ip(5) == ("172.16.5.1", "172.16.5.2")
