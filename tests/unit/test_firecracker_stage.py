from __future__ import annotations

import asyncio
import os
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
from mshkn.host.firecracker import (
    kill_firecracker_process as real_kill_firecracker_process,
)
from mshkn.host.firecracker import (
    start_firecracker_process as real_start_firecracker_process,
)
from mshkn.host.shell import ShellError
from mshkn.resources import Resources
from tests.support import ShellRecorder
from tests.unit.test_firecracker_process import _fake_binary, _survivors

CONFIG = Config(
    ssh_key_path=Path("/tmp/k"),
    kernel_path=Path("/opt/firecracker/vmlinux.bin"),
    thin_pool_name="mshkn-pool",
    thin_volume_sectors=16777216,
)


class FakeClient:
    """Stands in for FirecrackerClient; records API calls per instance.

    Every call also lands on the shared `timeline`, so a test can assert that
    the client is closed before the port wait rather than merely that it ends
    up closed.
    """

    instances: ClassVar[list[FakeClient]] = []
    fail_on: ClassVar[str | None] = None
    timeline: ClassVar[list[str]] = []

    def __init__(self, socket_path: str, *, transport: Any = None) -> None:
        self.socket_path = socket_path
        self.calls: list[tuple[str, Any]] = []
        self.closed = False
        FakeClient.instances.append(self)

    async def configure_and_boot(self, config: fc.FirecrackerConfig) -> None:
        self._maybe_fail("configure_and_boot")
        self.calls.append(("configure_and_boot", config))
        self._record("boot")

    async def load_snapshot(self, vmstate: str, memory: str, resume_vm: bool = True) -> None:
        self._maybe_fail("load_snapshot")
        self.calls.append(("load_snapshot", (vmstate, memory, resume_vm)))
        self._record("load")

    async def pause(self) -> None:
        self.calls.append(("pause", None))
        self._record("pause")

    async def resume(self) -> None:
        self.calls.append(("resume", None))
        self._record("resume")

    async def create_snapshot(self, vmstate: str, memory: str) -> None:
        self.calls.append(("create_snapshot", (vmstate, memory)))
        self._record("snapshot")

    async def close(self) -> None:
        self.closed = True
        self._record("close")

    def _record(self, event: str) -> None:
        FakeClient.timeline.append(f"{event}:{self.socket_path}")

    def _maybe_fail(self, name: str) -> None:
        if FakeClient.fail_on == name:
            FakeClient.fail_on = None
            raise httpx.ConnectError("firecracker gone")


Staged = tuple[FirecrackerHypervisor, ShellRecorder, list[str]]


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> Staged:
    """A hypervisor whose process, port wait, SSH hop, and API client are all fakes.

    Every stream writes to one `timeline`: shell commands as `run:<cmd>`, API
    calls as `boot:`/`load:`/`pause:`/`snapshot:`/`resume:`/`close:<socket>`, and
    the process helpers as `start:`/`wait:`/`kill:`. Orderings that span those
    streams are then ordinary index comparisons.
    """
    FakeClient.instances = []
    FakeClient.fail_on = None
    timeline: list[str] = []
    FakeClient.timeline = timeline
    run = ShellRecorder(taps={"tap254"}, timeline=timeline)

    async def start(socket_path: str, **kwargs: Any) -> int:
        timeline.append(f"start:{socket_path}")
        return 4242

    async def wait(ip: str, port: int, *, timeout: float, interval: float = 0.01) -> None:
        timeline.append(f"wait:{ip}:{port}:{timeout}")

    async def kill(pid: int) -> None:
        timeline.append(f"kill:{pid}")

    monkeypatch.setattr(fc, "start_firecracker_process", start)
    monkeypatch.setattr(fc, "wait_for_port", wait)
    monkeypatch.setattr(fc, "kill_firecracker_process", kill)
    monkeypatch.setattr(fc, "FirecrackerClient", FakeClient)
    hv = FirecrackerHypervisor(CONFIG, run=run)

    async def ssh_add_ip(final_vm_ip: str, final_host_ip: str) -> None:
        timeline.append(f"ssh:{final_vm_ip}:{final_host_ip}")

    monkeypatch.setattr(hv, "_ssh_add_ip", ssh_add_ip)
    return hv, run, timeline


def _lifecycle(timeline: list[str]) -> list[str]:
    """The timeline with the shell commands filtered out."""
    return [e for e in timeline if not e.startswith("run:")]


def _first(timeline: list[str], entry: str) -> int:
    assert entry in timeline, f"{entry!r} missing from {timeline}"
    return timeline.index(entry)


def _last(timeline: list[str], entry: str) -> int:
    """Staging commands repeat across cleanup, so the tail one is the interesting one."""
    assert entry in timeline, f"{entry!r} missing from {timeline}"
    return len(timeline) - 1 - timeline[::-1].index(entry)


SOCKET = "/tmp/fc-mshkn-comp-a.socket"


def _staging_table(volume_id: int) -> str:
    return (
        f"dmsetup create {STAGING_DRIVE_NAME} "
        f"--table '0 16777216 thin /dev/mapper/mshkn-pool {volume_id}'"
    )


STAGING_TABLE = _staging_table(7)


def _rename_chain(slot: int, disk_name: str = "mshkn-comp-a") -> str:
    return (
        f"ip link set {STAGING_TAP} name tap{slot} && "
        f"ip addr flush dev tap{slot} && "
        f"ip addr add 172.16.{slot}.1/30 dev tap{slot} && "
        f"ip neigh replace 172.16.{slot}.2 lladdr {STAGING_MAC} dev tap{slot} nud permanent && "
        f"iptables -I FORWARD -i tap{slot} -s 172.16.{slot}.2 ! -d 172.16.0.0/12 -j ACCEPT && "
        f"iptables -I FORWARD -i tap{slot} -s 172.16.{slot}.2 -d 172.16.0.0/12 -j DROP && "
        f"dmsetup rename {STAGING_DRIVE_NAME} {disk_name}"
    )


async def test_boot_runs_the_staging_chain_in_order(staged: Staged) -> None:
    hv, run, timeline = staged
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
    # The API client is closed before the port wait, and the rename chain only
    # runs once the guest has taken its final IP over SSH.
    assert _lifecycle(timeline) == [
        f"start:{SOCKET}",
        f"boot:{SOCKET}",
        f"close:{SOCKET}",
        f"wait:{STAGING_VM_IP}:22:30.0",
        "ssh:172.16.3.2:172.16.3.1",
    ]
    assert _first(timeline, "ssh:172.16.3.2:172.16.3.1") < _first(
        timeline, f"run:{_rename_chain(3)}"
    )
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
    hv, run, timeline = staged
    files = SnapshotFiles(vmstate=Path("/c/vmstate"), memory=Path("/c/memory"))
    vm = await hv.restore(slot=9, disk_volume_id=7, disk_name="mshkn-comp-a", snapshot=files)
    assert vm.slot == 9
    assert vm.tap_device == "tap9"
    (client,) = FakeClient.instances
    assert client.calls == [("load_snapshot", ("/c/vmstate", "/c/memory", True))]
    assert _lifecycle(timeline) == [
        f"start:{SOCKET}",
        f"load:{SOCKET}",
        f"close:{SOCKET}",
        f"wait:{STAGING_VM_IP}:22:5.0",
        "ssh:172.16.9.2:172.16.9.1",
    ]
    assert _rename_chain(9) in [c for c, _ in run.calls]


async def test_activate_failure_cleans_staging_and_raises_host_error(staged: Staged) -> None:
    hv, run, timeline = staged
    FakeClient.fail_on = "configure_and_boot"
    with pytest.raises(HostError):
        await hv.boot(slot=3, disk_volume_id=7, disk_name="mshkn-comp-a", resources=Resources())
    assert "kill:4242" in timeline, "the staged process is killed"
    assert f"close:{SOCKET}" in timeline, "the client is closed even when activate raises"
    assert _first(timeline, f"close:{SOCKET}") < _first(timeline, "kill:4242")
    cmds = [c for c, _ in run.calls]
    assert cmds.count(f"dmsetup remove {STAGING_DRIVE_NAME}") == 2, (
        "cleanup ran once before and once after"
    )
    assert f"ip link del {STAGING_TAP}" in cmds, "tap254 (present per the recorder) is torn down"
    assert not any("dmsetup rename" in c for c in cmds)


async def test_mapping_failure_cancels_the_process_start(
    staged: Staged, monkeypatch: pytest.MonkeyPatch
) -> None:
    hv, run, timeline = staged
    blocked = asyncio.Event()  # never set: the start only ends by cancellation

    async def blocking_start(socket_path: str, **kwargs: Any) -> int:
        timeline.append(f"start:{socket_path}")
        try:
            await blocked.wait()
        except asyncio.CancelledError:
            timeline.append("start-cancelled")
            raise
        return 4242

    monkeypatch.setattr(fc, "start_firecracker_process", blocking_start)
    run.responses[STAGING_TABLE] = ShellError(STAGING_TABLE, 1, "pool full")
    with pytest.raises(HostError):
        await hv.boot(slot=3, disk_volume_id=7, disk_name="mshkn-comp-a", resources=Resources())
    assert "start-cancelled" in timeline, "the in-flight Firecracker start is cancelled"
    assert FakeClient.instances == [], "no API client is ever built"
    assert not any(e.startswith("ssh:") for e in timeline)


async def test_build_template_boots_snapshots_and_tears_down_staging(
    staged: Staged, tmp_path: Path
) -> None:
    hv, run, timeline = staged
    files = await hv.build_template(disk_volume_id=0, dest_dir=tmp_path / "t")
    assert files == SnapshotFiles(
        vmstate=tmp_path / "t" / "vmstate", memory=tmp_path / "t" / "memory"
    )
    boot_client, snap_client = FakeClient.instances
    assert boot_client.closed and snap_client.closed
    assert [n for n, _ in snap_client.calls] == ["pause", "create_snapshot"]
    # The boot client is closed before the port wait, so no idle keep-alive is
    # held across the cold boot; a second client is opened for the snapshot.
    template = "/tmp/fc-template-0.socket"
    assert _lifecycle(timeline) == [
        f"start:{template}",
        f"boot:{template}",
        f"close:{template}",
        f"wait:{STAGING_VM_IP}:22:30.0",
        f"pause:{template}",
        f"snapshot:{template}",
        f"close:{template}",
        "kill:4242",
    ]
    # The process dies before the tap and the volume go, so it releases the fd first.
    assert _first(timeline, "kill:4242") < _last(timeline, f"run:ip link del {STAGING_TAP}")
    assert _first(timeline, "kill:4242") < _last(
        timeline, f"run:dmsetup remove {STAGING_DRIVE_NAME}"
    )
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
    first_rename = cmds.index(_rename_chain(1))
    second_table = cmds.index(_staging_table(8))
    assert first_rename < second_table, "the second boot's staging map waits for the first's rename"


async def test_kill_unlinks_the_api_socket_a_boot_created(
    staged: Staged, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A killed VM must leave no API socket behind.

    Firecracker does not remove its own socket on exit and
    `start_firecracker_process` only clears the one path it is about to reuse,
    so before this the live host held 1391 stale /tmp/fc-*.socket files against
    zero firecracker processes. The real start/kill helpers run here against
    the fake binary from the process tests, which touches the socket path, so
    the file under assertion is a real file. `_stage` hardcodes /tmp for the
    socket, hence the pid-qualified disk name rather than tmp_path.
    """
    hv, _run, _timeline = staged
    binary = _fake_binary(tmp_path, creates_socket=True)

    async def start(socket_path: str, **_kwargs: Any) -> int:
        return await real_start_firecracker_process(socket_path, binary=binary)

    monkeypatch.setattr(fc, "start_firecracker_process", start)
    monkeypatch.setattr(fc, "kill_firecracker_process", real_kill_firecracker_process)

    disk_name = f"mshkn-comp-sockettest-{os.getpid()}"
    socket_path = Path(f"/tmp/fc-{disk_name}.socket")
    try:
        vm = await hv.boot(slot=3, disk_volume_id=7, disk_name=disk_name, resources=Resources())
        assert vm.socket_path == str(socket_path)
        assert socket_path.exists(), "the fake firecracker bound its API socket"
        await hv.kill(vm.pid)
        assert not socket_path.exists(), "kill must unlink the API socket it recorded"
    finally:
        socket_path.unlink(missing_ok=True)
        socket_path.with_suffix(".socket.pid").unlink(missing_ok=True)
    assert _survivors(binary) == [], "the test leaves no process behind"


async def test_killing_a_pid_the_hypervisor_never_started_is_still_a_kill(
    staged: Staged,
) -> None:
    """`kill` of an unrecorded pid still kills and does not raise on the missing socket."""
    hv, _run, timeline = staged
    await hv.kill(4242)
    assert timeline == ["kill:4242"]


async def test_kill_of_a_vm_that_already_died_still_unlinks_its_socket(
    staged: Staged, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dead-VM reaper reaches kill() after the process is gone; the socket must still go.

    On the live host the one socket that survived a full E2E run belonged to a VM
    whose process had died on its own and was cleaned up by the reaper, which
    never called kill(). kill() on a dead pid is a no-op for the process and
    must still release the socket recorded for it.
    """
    hv, _run, _timeline = staged
    binary = _fake_binary(tmp_path, creates_socket=True)

    async def start(socket_path: str, **_kwargs: Any) -> int:
        return await real_start_firecracker_process(socket_path, binary=binary)

    monkeypatch.setattr(fc, "start_firecracker_process", start)
    monkeypatch.setattr(fc, "kill_firecracker_process", real_kill_firecracker_process)

    disk_name = f"mshkn-comp-sockettest-dead-{os.getpid()}"
    socket_path = Path(f"/tmp/fc-{disk_name}.socket")
    try:
        vm = await hv.boot(slot=3, disk_volume_id=7, disk_name=disk_name, resources=Resources())
        await real_kill_firecracker_process(vm.pid)  # the VM dies on its own
        assert not hv.is_alive(vm.pid)
        assert socket_path.exists(), "firecracker does not remove its socket when it dies"
        await hv.kill(vm.pid)
        assert not socket_path.exists(), "kill of a dead pid must still unlink its socket"
    finally:
        socket_path.unlink(missing_ok=True)
        socket_path.with_suffix(".socket.pid").unlink(missing_ok=True)
    assert _survivors(binary) == [], "the test leaves no process behind"


async def test_a_failed_boot_unlinks_the_socket_it_created(
    staged: Staged, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The staging cleanup path unlinks too, so a failed boot leaks nothing either."""
    hv, _run, _timeline = staged
    binary = _fake_binary(tmp_path, creates_socket=True)

    async def start(socket_path: str, **_kwargs: Any) -> int:
        return await real_start_firecracker_process(socket_path, binary=binary)

    monkeypatch.setattr(fc, "start_firecracker_process", start)
    monkeypatch.setattr(fc, "kill_firecracker_process", real_kill_firecracker_process)
    FakeClient.fail_on = "configure_and_boot"

    disk_name = f"mshkn-comp-sockettest-fail-{os.getpid()}"
    socket_path = Path(f"/tmp/fc-{disk_name}.socket")
    try:
        with pytest.raises(HostError):
            await hv.boot(slot=3, disk_volume_id=7, disk_name=disk_name, resources=Resources())
        assert not socket_path.exists(), "the cleanup path must unlink the API socket"
    finally:
        socket_path.unlink(missing_ok=True)
        socket_path.with_suffix(".socket.pid").unlink(missing_ok=True)
    assert _survivors(binary) == [], "the test leaves no process behind"
