"""Firecracker-backed hypervisor: the microVM half of the host boundary.

Holds the low-level Firecracker API client and process helpers, plus the
staging slot (254) that every boot and restore passes through.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import asyncssh
import httpx

from mshkn.errors import HostError
from mshkn.host import RunningVM, SnapshotFiles
from mshkn.host.network import create_tap, destroy_tap, slot_to_ip, slot_to_tap
from mshkn.host.shell import RunFn
from mshkn.host.shell import run as shell_run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mshkn.config import Config
    from mshkn.resources import Resources

logger = logging.getLogger(__name__)

_WRAPPED = (httpx.HTTPError, TimeoutError, OSError, asyncssh.Error)


@contextlib.asynccontextmanager
async def _host_errors(what: str) -> AsyncIterator[None]:
    """Turn transport-level failures into HostError; leave HostError alone."""
    try:
        yield
    except HostError:
        raise
    except _WRAPPED as exc:
        raise HostError(f"{what}: {type(exc).__name__}: {exc}") from exc


BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/init root=/dev/vda rw"

# Staging slot constants — must match the vmstate baked into templates
STAGING_SLOT = 254
STAGING_TAP = "tap254"
STAGING_HOST_IP = "172.16.254.1"
STAGING_VM_IP = "172.16.254.2"
STAGING_MAC = "06:00:AC:10:FE:02"
STAGING_DRIVE_NAME = "mshkn-restore-staging"

# TCP connect to a VM that was just killed otherwise hangs for the kernel's
# SYN retry budget, ~2 minutes.
CONNECT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class FirecrackerConfig:
    socket_path: str
    kernel_path: str
    rootfs_path: str
    tap_device: str
    guest_mac: str
    vcpu_count: int = 2
    mem_size_mib: int = 256
    boot_args: str = field(default=BOOT_ARGS)


class FirecrackerClient:
    """Async client for a single Firecracker instance via Unix socket API."""

    def __init__(
        self, socket_path: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.socket_path = socket_path
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://localhost",
        )

    async def configure_and_boot(self, config: FirecrackerConfig) -> None:
        await self._put(
            "/machine-config",
            {
                "vcpu_count": config.vcpu_count,
                "mem_size_mib": config.mem_size_mib,
            },
        )
        await self._put(
            "/boot-source",
            {
                "kernel_image_path": config.kernel_path,
                "boot_args": config.boot_args,
            },
        )
        await self._put(
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": config.rootfs_path,
                "is_root_device": True,
                "is_read_only": False,
            },
        )
        await self._put(
            "/network-interfaces/eth0",
            {
                "iface_id": "eth0",
                "guest_mac": config.guest_mac,
                "host_dev_name": config.tap_device,
            },
        )
        await self._put("/actions", {"action_type": "InstanceStart"})
        logger.info("Firecracker VM configured and started via %s", self.socket_path)

    async def pause(self) -> None:
        await self._patch("/vm", {"state": "Paused"})

    async def resume(self) -> None:
        await self._patch("/vm", {"state": "Resumed"})

    async def create_snapshot(self, snapshot_path: str, memory_path: str) -> None:
        await self._put(
            "/snapshot/create",
            {
                "snapshot_type": "Full",
                "snapshot_path": snapshot_path,
                "mem_file_path": memory_path,
            },
        )

    async def load_snapshot(
        self, snapshot_path: str, memory_path: str, resume_vm: bool = True
    ) -> None:
        await self._put(
            "/snapshot/load",
            {
                "snapshot_path": snapshot_path,
                "mem_backend": {
                    "backend_type": "File",
                    "backend_path": memory_path,
                },
                "resume_vm": resume_vm,
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _put(self, path: str, body: dict[str, object]) -> None:
        resp = await self._client.put(path, json=body)
        if resp.status_code not in (200, 204):
            logger.error("Firecracker PUT %s failed: %s %s", path, resp.status_code, resp.text)
            resp.raise_for_status()

    async def _patch(self, path: str, body: dict[str, object]) -> None:
        resp = await self._client.patch(path, json=body)
        if resp.status_code not in (200, 204):
            logger.error("Firecracker PATCH %s failed: %s %s", path, resp.status_code, resp.text)
            resp.raise_for_status()


async def start_firecracker_process(
    socket_path: str, *, binary: str = "firecracker", socket_timeout: float = 2.0
) -> int:
    """Start a Firecracker process and return its PID."""
    # Remove stale socket (in-process, avoids subprocess overhead)
    with contextlib.suppress(FileNotFoundError):
        Path(socket_path).unlink()

    proc = await asyncio.create_subprocess_exec(
        binary,
        "--api-sock",
        socket_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Poll for socket creation instead of fixed 500ms sleep
    loop = asyncio.get_running_loop()
    deadline = loop.time() + socket_timeout
    while loop.time() < deadline:
        if Path(socket_path).exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError(f"Firecracker socket {socket_path} not created within {socket_timeout}s")
    logger.info("Started Firecracker process PID=%d socket=%s", proc.pid, socket_path)
    return proc.pid


async def kill_firecracker_process(pid: int) -> None:
    """Kill a Firecracker process by PID and wait for it to exit."""
    try:
        os.kill(pid, signal.SIGKILL)
        logger.info("Killed Firecracker PID=%d", pid)
    except ProcessLookupError:
        logger.warning("Firecracker PID=%d already dead", pid)
        return

    # Wait for process to actually exit so it releases tap device fds
    for _ in range(20):  # up to 2s
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.1)
    logger.warning("Firecracker PID=%d still alive after 2s", pid)


async def wait_for_port(ip: str, port: int, *, timeout: float, interval: float = 0.01) -> None:
    """Poll until ip:port accepts TCP connections."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=0.05)
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, TimeoutError):
            pass
        await asyncio.sleep(interval)
    raise TimeoutError(f"{ip}:{port} did not become reachable within {timeout}s")


class FirecrackerHypervisor:
    """Boots, restores, snapshots, and kills Firecracker microVMs.

    Every boot/restore goes through the staging slot (254): the VM comes up
    with the staging tap and IP baked into templates and checkpoints, then
    is moved to its final slot via SSH and a tap rename. One restore at a
    time; the lock is an instance attribute, so a process must construct
    exactly one FirecrackerHypervisor (the staging slot is host-global).
    """

    _RESTORE_SSH_TIMEOUT = 5.0
    _BOOT_SSH_TIMEOUT = 30.0

    def __init__(self, config: Config, *, run: RunFn = shell_run) -> None:
        self._config = config
        self._run = run
        self._staging_lock = asyncio.Lock()

    # -- Hypervisor protocol -------------------------------------------------

    async def boot(
        self, *, slot: int, disk_volume_id: int, disk_name: str, resources: Resources
    ) -> RunningVM:
        async def activate(client: FirecrackerClient, socket_path: str) -> None:
            await client.configure_and_boot(
                FirecrackerConfig(
                    socket_path=socket_path,
                    kernel_path=str(self._config.kernel_path),
                    rootfs_path=f"/dev/mapper/{STAGING_DRIVE_NAME}",
                    tap_device=STAGING_TAP,
                    guest_mac=STAGING_MAC,
                    mem_size_mib=resources.mem_mib,
                    vcpu_count=resources.vcpus,
                )
            )

        async with _host_errors("boot"):
            return await self._stage(
                slot=slot,
                disk_volume_id=disk_volume_id,
                disk_name=disk_name,
                activate=activate,
                ssh_timeout=self._BOOT_SSH_TIMEOUT,
            )

    async def restore(
        self, *, slot: int, disk_volume_id: int, disk_name: str, snapshot: SnapshotFiles
    ) -> RunningVM:
        async def activate(client: FirecrackerClient, socket_path: str) -> None:  # noqa: ARG001
            await client.load_snapshot(str(snapshot.vmstate), str(snapshot.memory), resume_vm=True)

        async with _host_errors("restore"):
            return await self._stage(
                slot=slot,
                disk_volume_id=disk_volume_id,
                disk_name=disk_name,
                activate=activate,
                ssh_timeout=self._RESTORE_SSH_TIMEOUT,
            )

    async def snapshot(self, socket_path: str, dest_dir: Path) -> SnapshotFiles:
        """Pause, write vmstate+memory into dest_dir, resume."""
        async with _host_errors("snapshot"):
            dest_dir.mkdir(parents=True, exist_ok=True)
            files = SnapshotFiles(vmstate=dest_dir / "vmstate", memory=dest_dir / "memory")
            client = FirecrackerClient(socket_path)
            try:
                await client.pause()
                await client.create_snapshot(str(files.vmstate), str(files.memory))
                await client.resume()
            finally:
                await client.close()
            logger.info("VM snapshot created at %s", dest_dir)
            return files

    async def build_template(self, *, disk_volume_id: int, dest_dir: Path) -> SnapshotFiles:
        """Cold-boot the given disk on the staging slot, snapshot it there, and tear it down."""
        async with _host_errors("build_template"):
            dest_dir.mkdir(parents=True, exist_ok=True)
            files = SnapshotFiles(vmstate=dest_dir / "vmstate", memory=dest_dir / "memory")
            socket_path = f"/tmp/fc-template-{disk_volume_id}.socket"
            pid: int | None = None
            async with self._staging_lock:
                try:
                    await self._ensure_staging_clean()
                    await asyncio.gather(
                        self._map_staging_disk(disk_volume_id),
                        create_tap(STAGING_SLOT, run=self._run),
                    )
                    pid = await start_firecracker_process(socket_path)
                    client = FirecrackerClient(socket_path)
                    try:
                        await client.configure_and_boot(
                            FirecrackerConfig(
                                socket_path=socket_path,
                                kernel_path=str(self._config.kernel_path),
                                rootfs_path=f"/dev/mapper/{STAGING_DRIVE_NAME}",
                                tap_device=STAGING_TAP,
                                guest_mac=STAGING_MAC,
                            )
                        )
                    finally:
                        await client.close()
                    # The cold boot can take tens of seconds; do not hold an idle
                    # keep-alive connection to the Firecracker socket across it.
                    await wait_for_port(
                        STAGING_VM_IP, 22, timeout=self._BOOT_SSH_TIMEOUT, interval=0.025
                    )
                    client = FirecrackerClient(socket_path)
                    try:
                        await client.pause()
                        await client.create_snapshot(str(files.vmstate), str(files.memory))
                    finally:
                        await client.close()
                    await kill_firecracker_process(pid)
                    pid = None
                    await destroy_tap(STAGING_SLOT, run=self._run)
                    await self._run(f"dmsetup remove {STAGING_DRIVE_NAME}")
                except Exception:
                    logger.exception("Template build on volume %d failed", disk_volume_id)
                    await self._cleanup_staging(pid)
                    raise
            logger.info("Built template from volume %d at %s", disk_volume_id, dest_dir)
            return files

    async def kill(self, pid: int) -> None:
        await kill_firecracker_process(pid)

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def teardown_slot(self, slot: int) -> None:
        await destroy_tap(slot, run=self._run)

    # -- staging -------------------------------------------------------------

    async def _stage(
        self,
        *,
        slot: int,
        disk_volume_id: int,
        disk_name: str,
        activate: Callable[[FirecrackerClient, str], Awaitable[None]],
        ssh_timeout: float,
    ) -> RunningVM:
        final_host_ip, final_vm_ip = slot_to_ip(slot)
        final_tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{disk_name}.socket"
        pid: int | None = None
        async with self._staging_lock:
            try:
                await self._ensure_staging_clean()
                fc_task = asyncio.create_task(start_firecracker_process(socket_path))
                try:
                    await asyncio.gather(
                        self._map_staging_disk(disk_volume_id),
                        create_tap(STAGING_SLOT, run=self._run),
                    )
                except Exception:
                    fc_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await fc_task
                    raise
                pid = await fc_task
                client = FirecrackerClient(socket_path)
                try:
                    await activate(client, socket_path)
                finally:
                    await client.close()
                await wait_for_port(STAGING_VM_IP, 22, timeout=ssh_timeout)
                await asyncio.gather(
                    self._ssh_add_ip(final_vm_ip, final_host_ip),
                    self._run(f"ip link del {final_tap}", check=False),
                )
                await self._run(
                    f"ip link set {STAGING_TAP} name {final_tap} && "
                    f"ip addr flush dev {final_tap} && "
                    f"ip addr add {final_host_ip}/30 dev {final_tap} && "
                    f"ip neigh replace {final_vm_ip} lladdr {STAGING_MAC} "
                    f"dev {final_tap} nud permanent && "
                    f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} "
                    f"! -d 172.16.0.0/12 -j ACCEPT && "
                    f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} "
                    f"-d 172.16.0.0/12 -j DROP && "
                    f"dmsetup rename {STAGING_DRIVE_NAME} {disk_name}"
                )
            except Exception:
                await self._cleanup_staging(pid)
                raise
        return RunningVM(
            pid=pid, socket_path=socket_path, slot=slot, vm_ip=final_vm_ip, tap_device=final_tap
        )

    async def _map_staging_disk(self, disk_volume_id: int) -> None:
        # Staging shells dm-thin directly so the mapping stays under the
        # staging lock. The table string is the same one DmThinBlockStore
        # builds: a change to the sector count must be made in dmthin.py too.
        await self._run(
            f"dmsetup create {STAGING_DRIVE_NAME} "
            f"--table '0 {self._config.thin_volume_sectors} thin "
            f"/dev/mapper/{self._config.thin_pool_name} {disk_volume_id}'"
        )

    async def _ensure_staging_clean(self) -> None:
        """Remove stale staging resources from a previous failed restore, quietly."""
        try:
            await destroy_tap(STAGING_SLOT, run=self._run)
        except Exception:
            logger.debug("Staging tap cleanup failed", exc_info=True)
        try:
            await self._run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)
        except Exception:
            logger.debug("Staging drive cleanup failed", exc_info=True)

    async def _cleanup_staging(self, pid: int | None) -> None:
        if pid is not None:
            try:
                await kill_firecracker_process(pid)
            except Exception:
                logger.warning("Failed to kill staging FC process PID=%s", pid)
        await self._ensure_staging_clean()

    async def _ssh_add_ip(self, final_vm_ip: str, final_host_ip: str) -> None:
        """Give the guest its final IP and default route, through the staging IP.

        The staging IP is left on the VM — once tap254 is renamed, the old IP
        is unreachable anyway (no matching tap/subnet on the host). `ip addr
        add` may fail with EEXIST when a fork reuses the parent's slot.
        """
        conn = await asyncio.wait_for(
            asyncssh.connect(
                STAGING_VM_IP,
                username="root",
                known_hosts=None,
                client_keys=[str(self._config.ssh_key_path)],
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        async with conn:
            await conn.run(
                f"ip addr add {final_vm_ip}/30 dev eth0 2>/dev/null; "
                f"ip route replace default via {final_host_ip} && "
                f"ip neigh flush dev eth0",
                check=True,
            )
