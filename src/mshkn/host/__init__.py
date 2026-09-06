"""The host boundary.

Everything the orchestrator does to the machine goes through one of five
protocols. Production uses the Firecracker-backed implementations in this
package; tests use the in-memory fakes in host/fake.py. Nothing in this
package imports api, vm, db, or runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from mshkn.resources import Resources

StreamName = Literal["stdout", "stderr", "exit"]
OutputLine = tuple[StreamName, str]


@dataclass(frozen=True)
class RunningVM:
    pid: int
    socket_path: str
    slot: int
    vm_ip: str
    tap_device: str


@dataclass(frozen=True)
class SnapshotFiles:
    vmstate: Path
    memory: Path


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class VmMetrics:
    cpu_pct: float
    ram_usage_mb: int
    ram_total_mb: int
    disk_usage_mb: int
    disk_total_mb: int
    processes: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class PoolUsage:
    data_used_ratio: float
    metadata_used_ratio: float


class Hypervisor(Protocol):
    async def boot(
        self, *, slot: int, disk_volume_id: int, disk_name: str, resources: Resources
    ) -> RunningVM: ...
    async def restore(
        self, *, slot: int, disk_volume_id: int, disk_name: str, snapshot: SnapshotFiles
    ) -> RunningVM: ...
    async def snapshot(self, socket_path: str, dest_dir: Path) -> SnapshotFiles: ...
    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles: ...
    async def kill(self, pid: int) -> None: ...
    def is_alive(self, pid: int) -> bool: ...
    async def teardown_slot(self, slot: int) -> None: ...


class BlockStore(Protocol):
    async def snap(self, *, source_volume_id: int, new_volume_id: int) -> None: ...
    async def activate(self, *, volume_id: int, name: str) -> None: ...
    async def deactivate(self, name: str) -> None: ...
    async def remove(self, *, volume_id: int, name: str) -> None: ...
    async def mkfs(self, name: str) -> None: ...
    def mounted(
        self, name: str, *, readonly: bool = False
    ) -> AbstractAsyncContextManager[Path]: ...
    async def max_volume_id(self) -> int | None: ...
    async def usage(self) -> PoolUsage: ...


class Guest(Protocol):
    async def exec(self, vm_ip: str, command: str, *, timeout: float = 300.0) -> ExecResult: ...
    def stream(
        self, vm_ip: str, command: str, *, timeout: float = 60.0
    ) -> AsyncIterator[OutputLine]: ...
    async def exec_bg(self, vm_ip: str, command: str) -> int: ...
    async def upload(self, vm_ip: str, remote_path: str, data: bytes) -> None: ...
    async def download(self, vm_ip: str, remote_path: str) -> bytes: ...
    async def metrics(self, vm_ip: str, *, timeout: float = 10.0) -> VmMetrics: ...
    async def warm(self, vm_ip: str) -> None: ...
    async def evict(self, vm_ip: str) -> None: ...
    async def close(self) -> None: ...


class ObjectStore(Protocol):
    async def upload_dir(self, local_dir: Path, prefix: str) -> None: ...
    async def download_dir(self, prefix: str, local_dir: Path) -> None: ...
    async def delete_prefix(self, prefix: str) -> None: ...


class Proxy(Protocol):
    async def add_route(self, computer_id: str, vm_ip: str) -> None: ...
    async def remove_route(self, computer_id: str) -> None: ...
    async def healthy(self) -> bool: ...
    async def close(self) -> None: ...


@dataclass
class Host:
    hypervisor: Hypervisor
    blocks: BlockStore
    guest: Guest
    objects: ObjectStore
    proxy: Proxy
