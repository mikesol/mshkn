"""Staging slot for Firecracker snapshot restore.

All LOAD_SNAPSHOT restores go through a dedicated staging slot (254).
After restore, the VM's network is reconfigured to its final slot via SSH.
An asyncio.Lock serializes restores (~50ms each).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from mshkn.shell import run
from mshkn.vm.firecracker import (
    FirecrackerClient,
    kill_firecracker_process,
    start_firecracker_process,
)
from mshkn.vm.network import create_tap, destroy_tap, slot_to_ip, slot_to_tap

logger = logging.getLogger(__name__)

# Staging slot constants — must match the vmstate baked into templates
STAGING_SLOT = 254
STAGING_TAP = "tap254"
STAGING_HOST_IP = "172.16.254.1"
STAGING_VM_IP = "172.16.254.2"
STAGING_MAC = "06:00:AC:10:FE:02"
STAGING_DRIVE_NAME = "mshkn-restore-staging"

# Global lock — only one restore at a time through the staging slot
_restore_lock = asyncio.Lock()


@dataclass
class RestoreResult:
    """Result of a staging restore — the VM is running on its final slot."""
    pid: int
    socket_path: str
    vm_ip: str
    tap_device: str


async def restore_from_snapshot(
    vmstate_path: str,
    memory_path: str,
    disk_volume_id: int,
    final_slot: int,
    pool_name: str,
    thin_volume_sectors: int,
    final_volume_name: str,
) -> RestoreResult:
    """Restore a VM from a Firecracker snapshot via the staging slot.

    1. Map disk as staging drive name
    2. Create staging tap
    3. Start FC + LOAD_SNAPSHOT
    4. Wait for SSH at staging IP
    5. Add final IP to guest via SSH
    6. Rename tap + reconfigure host side + rename drive
    7. Verify VM reachable at final IP

    All VMs go through staging during creation, so they always have
    STAGING_VM_IP (172.16.254.2) on eth0. We SSH through this IP for
    both template restores and fork restores to avoid route conflicts
    with the parent VM's TAP device.

    The caller is responsible for:
    - Creating the dm-thin snapshot (disk_volume_id) in the pool before calling this
    - Recording the Computer in the DB after this returns
    """
    final_host_ip, final_vm_ip = slot_to_ip(final_slot)
    final_tap = slot_to_tap(final_slot)
    socket_path = f"/tmp/fc-staging-{final_slot}.socket"
    pid: int | None = None

    async with _restore_lock:
        try:
            # 1. Map disk volume as staging drive name
            await run(
                f"dmsetup create {STAGING_DRIVE_NAME} "
                f"--table '0 {thin_volume_sectors} thin /dev/mapper/{pool_name} {disk_volume_id}'"
            )

            # 2. Create staging tap (tap254 with 172.16.254.x)
            await create_tap(STAGING_SLOT)

            # 3. Start FC + LOAD_SNAPSHOT
            pid = await start_firecracker_process(socket_path)
            fc_client = FirecrackerClient(socket_path)
            try:
                await fc_client.load_snapshot(vmstate_path, memory_path, resume_vm=True)
            finally:
                await fc_client.close()

            # 4. Wait for SSH at staging IP
            await _wait_for_ssh_staging(STAGING_VM_IP)

            # 5. Add final IP + update default route via SSH
            await _ssh_add_ip(STAGING_VM_IP, final_vm_ip, final_host_ip)

            # 6. Rename tap + reconfigure host side + rename drive
            # Delete stale final tap if it exists (from a previous failed restore),
            # then rename staging tap to final name.
            await run(f"ip link del {final_tap}", check=False)
            await run(
                f"ip link set {STAGING_TAP} name {final_tap} && "
                f"ip addr flush dev {final_tap} && "
                f"ip addr add {final_host_ip}/30 dev {final_tap} && "
                f"ip neigh replace {final_vm_ip} lladdr {STAGING_MAC} "
                f"dev {final_tap} nud permanent && "
                f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} "
                f"! -d 172.16.0.0/12 -j ACCEPT && "
                f"iptables -I FORWARD -i {final_tap} -s {final_vm_ip} "
                f"-d 172.16.0.0/12 -j DROP && "
                f"dmsetup rename {STAGING_DRIVE_NAME} {final_volume_name}"
            )

            # 7. Verify VM is reachable at final IP before returning
            await _wait_for_ssh_staging(final_vm_ip)

        except Exception:
            # Cleanup on failure
            await _cleanup_staging(pid=pid)
            raise

    return RestoreResult(
        pid=pid,
        socket_path=socket_path,
        vm_ip=final_vm_ip,
        tap_device=final_tap,
    )


async def _cleanup_staging(pid: int | None = None) -> None:
    """Best-effort cleanup of staging resources after a failed restore."""
    if pid is not None:
        try:
            await kill_firecracker_process(pid)
        except Exception:
            logger.warning("Failed to kill staging FC process PID=%s", pid)

    with contextlib.suppress(Exception):
        await destroy_tap(STAGING_SLOT)

    with contextlib.suppress(Exception):
        await run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)


async def _wait_for_ssh_staging(vm_ip: str, timeout: float = 5.0) -> None:
    """Poll until VM port 22 accepts TCP connections. Short timeout for snapshot restore."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(vm_ip, 22),
                timeout=0.05,
            )
            writer.close()
            await writer.wait_closed()
            return
        except (OSError, TimeoutError):
            pass
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Staging VM at {vm_ip} did not become reachable on port 22")


async def _ssh_add_ip(connect_ip: str, final_vm_ip: str, final_host_ip: str) -> None:
    """Add the final IP and update default route via SSH.

    The source IP is left on the VM — once tap254 is renamed, the old IP is
    unreachable anyway (no matching tap/subnet on the host). This avoids the
    complexity of removing an IP that we're connected through.
    """
    import asyncssh

    async with asyncssh.connect(
        connect_ip,
        username="root",
        known_hosts=None,
        client_keys=["/root/.ssh/id_ed25519"],
    ) as conn:
        # ip addr add may fail with EEXIST if the fork VM already has
        # this IP (happens when fork reuses the parent's slot).
        await conn.run(
            f"ip addr add {final_vm_ip}/30 dev eth0 2>/dev/null; "
            f"ip route replace default via {final_host_ip} && "
            f"ip neigh flush dev eth0",
            check=True,
        )
