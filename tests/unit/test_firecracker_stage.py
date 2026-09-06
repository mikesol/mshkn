from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

import mshkn.host.firecracker as fc
from mshkn.config import Config
from mshkn.errors import HostError
from mshkn.host import RunningVM, SnapshotFiles
from mshkn.host.firecracker import (
    STAGING_DRIVE_NAME,
    STAGING_MAC,
    STAGING_TAP,
    STAGING_VM_IP,
    FirecrackerHypervisor,
)
from mshkn.host.shell import ShellError
from mshkn.resources import Resources
from tests.support import ShellRecorder

CONFIG = Config(
    ssh_key_path=Path("/tmp/k"),
    kernel_path=Path("/opt/firecracker/vmlinux.bin"),
    thin_pool_name="mshkn-pool",
    thin_volume_sectors=16777216,
)


class FakeClient:
    """Stands in for FirecrackerClient; records API calls per instance."""

    instances: ClassVar[list[FakeClient]] = []
    fail_on: ClassVar[str | None] = None

    def __init__(self, socket_path: str, *, transport: Any = None) -> None:
        self.socket_path = socket_path
        self.calls: list[tuple[str, Any]] = []
        self.closed = False
        FakeClient.instances.append(self)

    async def configure_and_boot(self, config: fc.FirecrackerConfig) -> None:
        self._maybe_fail("configure_and_boot")
        self.calls.append(("configure_and_boot", config))

    async def load_snapshot(self, vmstate: str, memory: str, resume_vm: bool = True) -> None:
        self._maybe_fail("load_snapshot")
        self.calls.append(("load_snapshot", (vmstate, memory, resume_vm)))

    async def pause(self) -> None:
        self.calls.append(("pause", None))

    async def resume(self) -> None:
        self.calls.append(("resume", None))

    async def create_snapshot(self, vmstate: str, memory: str) -> None:
        self.calls.append(("create_snapshot", (vmstate, memory)))

    async def close(self) -> None:
        self.closed = True

    def _maybe_fail(self, name: str) -> None:
        if FakeClient.fail_on == name:
            FakeClient.fail_on = None
            raise httpx.ConnectError("firecracker gone")


Staged = tuple[FirecrackerHypervisor, ShellRecorder, list[str]]


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> Staged:
    """A hypervisor whose process, port wait, SSH hop, and API client are all fakes.

    `events` records the cross-cutting order: process start, port wait, ssh hop, kill.
    """
    FakeClient.instances = []
    FakeClient.fail_on = None
    events: list[str] = []
    run = ShellRecorder(taps={"tap254"})

    async def start(socket_path: str, **kwargs: Any) -> int:
        events.append(f"start:{socket_path}")
        return 4242

    async def wait(ip: str, port: int, *, timeout: float, interval: float = 0.01) -> None:
        events.append(f"wait:{ip}:{port}:{timeout}")

    async def kill(pid: int) -> None:
        events.append(f"kill:{pid}")

    monkeypatch.setattr(fc, "start_firecracker_process", start)
    monkeypatch.setattr(fc, "wait_for_port", wait)
    monkeypatch.setattr(fc, "kill_firecracker_process", kill)
    monkeypatch.setattr(fc, "FirecrackerClient", FakeClient)
    hv = FirecrackerHypervisor(CONFIG, run=run)

    async def ssh_add_ip(final_vm_ip: str, final_host_ip: str) -> None:
        events.append(f"ssh:{final_vm_ip}:{final_host_ip}")

    monkeypatch.setattr(hv, "_ssh_add_ip", ssh_add_ip)
    return hv, run, events


STAGING_TABLE = (
    f"dmsetup create {STAGING_DRIVE_NAME} --table '0 16777216 thin /dev/mapper/mshkn-pool 7'"
)


def _rename_chain(slot: int) -> str:
    return (
        f"ip link set {STAGING_TAP} name tap{slot} && "
        f"ip addr flush dev tap{slot} && "
        f"ip addr add 172.16.{slot}.1/30 dev tap{slot} && "
        f"ip neigh replace 172.16.{slot}.2 lladdr {STAGING_MAC} dev tap{slot} nud permanent && "
        f"iptables -I FORWARD -i tap{slot} -s 172.16.{slot}.2 ! -d 172.16.0.0/12 -j ACCEPT && "
        f"iptables -I FORWARD -i tap{slot} -s 172.16.{slot}.2 -d 172.16.0.0/12 -j DROP && "
        f"dmsetup rename {STAGING_DRIVE_NAME} mshkn-comp-a"
    )


async def test_boot_runs_the_staging_chain_in_order(staged: Staged) -> None:
    hv, run, events = staged
    vm = await hv.boot(
        slot=3,
        disk_volume_id=7,
        disk_name="mshkn-comp-a",
        resources=Resources(mem_mib=512, vcpus=1),
    )
    assert vm == RunningVM(
        pid=4242,
        socket_path="/tmp/fc-mshkn-comp-a.socket",
        slot=3,
        vm_ip="172.16.3.2",
        tap_device="tap3",
    )
    cmds = [c for c, _ in run.calls]
    # staging cleaned, disk mapped, tap254 created, final tap cleared, rename chain, in that order
    assert cmds.index(f"dmsetup remove {STAGING_DRIVE_NAME}") < cmds.index(STAGING_TABLE)
    assert STAGING_TABLE in cmds
    assert f"ip tuntap add dev {STAGING_TAP} mode tap" in cmds
    assert cmds.index("ip link del tap3") < cmds.index(_rename_chain(3))
    assert _rename_chain(3) in cmds
    assert events == [
        "start:/tmp/fc-mshkn-comp-a.socket",
        f"wait:{STAGING_VM_IP}:22:30.0",
        "ssh:172.16.3.2:172.16.3.1",
    ]
    (client,) = FakeClient.instances
    assert client.closed
    (name, config) = client.calls[0]
    assert name == "configure_and_boot"
    assert (config.rootfs_path, config.tap_device, config.guest_mac) == (
        f"/dev/mapper/{STAGING_DRIVE_NAME}",
        STAGING_TAP,
        STAGING_MAC,
    )
    assert (config.mem_size_mib, config.vcpu_count, config.kernel_path) == (
        512,
        1,
        "/opt/firecracker/vmlinux.bin",
    )


async def test_restore_loads_the_snapshot_with_the_short_ssh_timeout(staged: Staged) -> None:
    hv, run, events = staged
    files = SnapshotFiles(vmstate=Path("/c/vmstate"), memory=Path("/c/memory"))
    vm = await hv.restore(slot=9, disk_volume_id=7, disk_name="mshkn-comp-a", snapshot=files)
    assert vm.slot == 9
    assert vm.tap_device == "tap9"
    (client,) = FakeClient.instances
    assert client.calls == [("load_snapshot", ("/c/vmstate", "/c/memory", True))]
    assert events[1] == f"wait:{STAGING_VM_IP}:22:5.0"
    assert _rename_chain(9) in [c for c, _ in run.calls]


async def test_activate_failure_cleans_staging_and_raises_host_error(staged: Staged) -> None:
    hv, run, events = staged
    FakeClient.fail_on = "configure_and_boot"
    with pytest.raises(HostError):
        await hv.boot(slot=3, disk_volume_id=7, disk_name="mshkn-comp-a", resources=Resources())
    assert "kill:4242" in events, "the staged process is killed"
    cmds = [c for c, _ in run.calls]
    assert cmds.count(f"dmsetup remove {STAGING_DRIVE_NAME}") == 2, (
        "cleanup ran once before and once after"
    )
    assert f"ip link del {STAGING_TAP}" in cmds, "tap254 (present per the recorder) is torn down"
    assert not any("dmsetup rename" in c for c in cmds)


async def test_mapping_failure_cancels_the_process_start(staged: Staged) -> None:
    hv, run, events = staged
    run.responses[STAGING_TABLE] = ShellError(STAGING_TABLE, 1, "pool full")
    with pytest.raises(HostError):
        await hv.boot(slot=3, disk_volume_id=7, disk_name="mshkn-comp-a", resources=Resources())
    assert FakeClient.instances == [], "no API client is ever built"
    assert not any(e.startswith("ssh:") for e in events)


async def test_build_template_boots_snapshots_and_tears_down_staging(
    staged: Staged, tmp_path: Path
) -> None:
    hv, run, events = staged
    files = await hv.build_template(disk_volume_id=0, dest_dir=tmp_path / "t")
    assert files == SnapshotFiles(
        vmstate=tmp_path / "t" / "vmstate", memory=tmp_path / "t" / "memory"
    )
    boot_client, snap_client = FakeClient.instances
    assert boot_client.closed and snap_client.closed, (
        "two clients; the boot one is closed before the port wait"
    )
    assert [n for n, _ in snap_client.calls] == ["pause", "create_snapshot"]
    assert events == [
        "start:/tmp/fc-template-0.socket",
        f"wait:{STAGING_VM_IP}:22:30.0",
        "kill:4242",
    ]
    cmds = [c for c, _ in run.calls]
    assert cmds[-1] == f"dmsetup remove {STAGING_DRIVE_NAME}"
    assert f"ip link del {STAGING_TAP}" in cmds[-4:]


async def test_snapshot_pauses_creates_and_resumes(staged: Staged, tmp_path: Path) -> None:
    hv, _, _ = staged
    files = await hv.snapshot("/tmp/fc-mshkn-comp-a.socket", tmp_path / "s")
    (client,) = FakeClient.instances
    assert [n for n, _ in client.calls] == ["pause", "create_snapshot", "resume"]
    assert client.calls[1][1] == (str(files.vmstate), str(files.memory))
    assert client.closed


async def test_two_boots_serialise_on_the_staging_lock(staged: Staged) -> None:
    hv, run, _ = staged
    await asyncio.gather(
        hv.boot(slot=1, disk_volume_id=7, disk_name="mshkn-comp-a", resources=Resources()),
        hv.boot(slot=2, disk_volume_id=8, disk_name="mshkn-comp-b", resources=Resources()),
    )
    cmds = [c for c, _ in run.calls]
    first_rename = next(
        i for i, c in enumerate(cmds) if c.startswith("ip link set tap254 name tap1")
    )
    second_table = next(i for i, c in enumerate(cmds) if c.endswith(" 8'"))
    assert first_rename < second_table, "the second boot's staging map waits for the first's rename"
