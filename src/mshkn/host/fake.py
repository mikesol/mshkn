"""In-memory implementations of the host protocols for flow tests and local runs.

Each fake records what was asked of it, keeps just enough state to make the
orchestrator's bookkeeping observable, and can be told to fail its next call
of a given method with `fail_next("<method>")` where the real implementation
can fail. Methods that the real code makes best-effort (never raising) stay
best-effort here; the fake then leaves the state behind, as the host would.
"""

from __future__ import annotations

import contextlib
import itertools
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.errors import HostError
from mshkn.host import (
    ExecResult,
    Host,
    OutputLine,
    PoolUsage,
    RunningVM,
    SnapshotFiles,
    VmMetrics,
)
from mshkn.host.network import slot_to_ip, slot_to_tap

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mshkn.resources import Resources


class _Failable:
    def __init__(self) -> None:
        self._fail: set[str] = set()

    def fail_next(self, method: str) -> None:
        self._fail.add(method)

    def _maybe_fail(self, method: str) -> None:
        if method in self._fail:
            self._fail.discard(method)
            raise HostError(f"fake {type(self).__name__}.{method} failed on request")


class FakeBlockStore(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.volumes: dict[int, int | None] = {0: None}  # id -> parent id
        self.active: dict[str, int] = {}  # device name -> volume id
        self.pool_usage = PoolUsage(data_used_ratio=0.1, metadata_used_ratio=0.05)
        self.calls: list[tuple[str, object]] = []

    # dm-thin refuses unknown ids, duplicate ids, and duplicate device names;
    # the fake does too, so ordering bugs in callers surface here instead of
    # only on the live pool.

    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None:
        self._maybe_fail("snap")
        if source_volume_id not in self.volumes:
            msg = f"fake snap: source volume {source_volume_id} does not exist"
            raise HostError(msg)
        if new_volume_id in self.volumes:
            msg = f"fake snap: volume {new_volume_id} already exists"
            raise HostError(msg)
        self.calls.append(("snap", (source_volume_id, new_volume_id)))
        self.volumes[new_volume_id] = source_volume_id

    async def activate(self, *, volume_id: int, name: str) -> None:
        self._maybe_fail("activate")
        if volume_id not in self.volumes:
            msg = f"fake activate: volume {volume_id} does not exist"
            raise HostError(msg)
        if name in self.active:
            msg = f"fake activate: device {name} already exists"
            raise HostError(msg)
        self.active[name] = volume_id

    async def deactivate(self, name: str) -> None:
        if name not in self.active:
            msg = f"fake deactivate: device {name} is not active"
            raise HostError(msg)
        del self.active[name]

    async def remove(self, *, volume_id: int, name: str) -> None:
        # The real remove is best-effort and never raises: a failed dmsetup
        # call is logged and the volume is left behind. fail_next("remove")
        # models exactly that.
        self.calls.append(("remove", (volume_id, name)))
        if "remove" in self._fail:
            self._fail.discard("remove")
            return
        self.active.pop(name, None)
        self.volumes.pop(volume_id, None)

    async def mkfs(self, name: str) -> None:
        self.calls.append(("mkfs", name))

    @contextlib.asynccontextmanager
    async def mounted(self, name: str, *, readonly: bool = False) -> AsyncIterator[Path]:  # noqa: ARG002
        if name not in self.active:
            msg = f"fake mounted: device {name} is not active"
            raise HostError(msg)
        path = Path(tempfile.mkdtemp(prefix=f"fake-mnt-{name}-"))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    async def max_volume_id(self) -> int | None:
        ids = [v for v in self.volumes if v != 0]
        return max(ids) if ids else None

    async def usage(self) -> PoolUsage:
        return self.pool_usage


class FakeHypervisor(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self._pids = itertools.count(1000)
        self.alive: dict[int, RunningVM] = {}
        self.booted: list[tuple[int, Resources]] = []
        self.restored: list[tuple[int, SnapshotFiles]] = []
        self.snapshots: list[tuple[str, Path]] = []
        self.torn_down: list[int] = []

    def _vm(self, slot: int, disk_name: str) -> RunningVM:
        pid = next(self._pids)
        _, vm_ip = slot_to_ip(slot)
        vm = RunningVM(
            pid=pid,
            socket_path=f"/tmp/fake-{disk_name}.socket",
            slot=slot,
            vm_ip=vm_ip,
            tap_device=slot_to_tap(slot),
        )
        self.alive[pid] = vm
        return vm

    async def boot(
        self, *, slot: int, disk_volume_id: int, disk_name: str, resources: Resources
    ) -> RunningVM:
        self._maybe_fail("boot")
        self.booted.append((disk_volume_id, resources))
        return self._vm(slot, disk_name)

    async def restore(
        self, *, slot: int, disk_volume_id: int, disk_name: str, snapshot: SnapshotFiles
    ) -> RunningVM:
        self._maybe_fail("restore")
        self.restored.append((disk_volume_id, snapshot))
        return self._vm(slot, disk_name)

    async def snapshot(self, socket_path: str, dest_dir: Path) -> SnapshotFiles:
        self._maybe_fail("snapshot")
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = SnapshotFiles(vmstate=dest_dir / "vmstate", memory=dest_dir / "memory")
        files.vmstate.write_bytes(b"fake-vmstate")
        files.memory.write_bytes(b"fake-memory")
        self.snapshots.append((socket_path, dest_dir))
        return files

    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles:
        self._maybe_fail("build_template")
        return await self.snapshot(f"/tmp/fake-template-{disk_volume_id}.socket", dest_dir)

    async def kill(self, pid: int) -> None:
        self.alive.pop(pid, None)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    async def teardown_slot(self, slot: int) -> None:
        self.torn_down.append(slot)


class FakeGuest(_Failable):
    """In-memory Guest.

    ``stream_script`` maps a command to the lines it yields. A script may end
    with its own ``("exit", code)`` line to model a non-zero exit; if it does
    not, a clean ``("exit", "0")`` is appended.
    """

    def __init__(self) -> None:
        super().__init__()
        self.script: dict[str, ExecResult] = {}
        self.stream_script: dict[str, list[OutputLine]] = {}
        self.commands: list[tuple[str, str]] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.warmed: list[str] = []
        self.evicted: list[str] = []
        self.default = ExecResult(exit_code=0, stdout="", stderr="")
        self.default_metrics = VmMetrics(
            cpu_pct=1.5,
            ram_usage_mb=64,
            ram_total_mb=230,
            disk_usage_mb=200,
            disk_total_mb=7800,
            processes=[{"pid": 1, "command": "systemd"}],
        )
        self._bg_pids = itertools.count(4000)

    async def exec(
        self,
        vm_ip: str,
        command: str,
        *,
        timeout: float = 300.0,  # noqa: ARG002
    ) -> ExecResult:
        self._maybe_fail("exec")
        self.commands.append((vm_ip, command))
        return self.script.get(command, self.default)

    async def stream(
        self,
        vm_ip: str,
        command: str,
        *,
        timeout: float = 60.0,  # noqa: ARG002
    ) -> AsyncIterator[OutputLine]:
        self._maybe_fail("stream")
        self.commands.append((vm_ip, command))
        items = self.stream_script.get(command, [])
        for item in items:
            yield item
        if not items or items[-1][0] != "exit":
            yield ("exit", "0")

    async def exec_bg(self, vm_ip: str, command: str) -> int:
        self._maybe_fail("exec_bg")
        self.commands.append((vm_ip, command))
        return next(self._bg_pids)

    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None:
        self._maybe_fail("upload")
        self.files[(vm_ip, remote_path)] = data

    async def download(self, vm_ip: str, remote_path: str) -> bytes:
        self._maybe_fail("download")
        try:
            return self.files[(vm_ip, remote_path)]
        except KeyError:
            raise FileNotFoundError(f"File not found: {remote_path}") from None

    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics:  # noqa: ARG002
        self._maybe_fail("metrics")
        return self.default_metrics

    async def warm(self, vm_ip: str) -> None:
        self.warmed.append(vm_ip)

    async def evict(self, vm_ip: str) -> None:
        self.evicted.append(vm_ip)

    async def close(self) -> None:
        return None


class FakeObjectStore(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.prefixes: dict[str, dict[str, bytes]] = {}

    async def upload_dir(self, local_dir: Path, prefix: str) -> None:
        self._maybe_fail("upload_dir")
        self.prefixes[prefix] = {p.name: p.read_bytes() for p in local_dir.iterdir() if p.is_file()}

    async def download_dir(self, prefix: str, local_dir: Path) -> None:
        self._maybe_fail("download_dir")
        local_dir.mkdir(parents=True, exist_ok=True)
        for name, data in self.prefixes.get(prefix, {}).items():
            (local_dir / name).write_bytes(data)

    async def delete_prefix(self, prefix: str) -> None:
        self.prefixes.pop(prefix, None)


class FakeProxy(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, str] = {}
        self.is_healthy = True

    async def add_route(self, computer_id: str, vm_ip: str) -> None:
        self._maybe_fail("add_route")
        self.routes[computer_id] = vm_ip

    async def remove_route(self, computer_id: str) -> None:
        self.routes.pop(computer_id, None)

    async def healthy(self) -> bool:
        return self.is_healthy

    async def close(self) -> None:
        return None


@dataclass
class FakeHostInstance(Host):
    hypervisor: FakeHypervisor
    blocks: FakeBlockStore
    guest: FakeGuest
    objects: FakeObjectStore
    proxy: FakeProxy


def FakeHost() -> FakeHostInstance:  # noqa: N802 — reads as a constructor at call sites
    return FakeHostInstance(
        hypervisor=FakeHypervisor(),
        blocks=FakeBlockStore(),
        guest=FakeGuest(),
        objects=FakeObjectStore(),
        proxy=FakeProxy(),
    )
