from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.db import (
    get_computer,
    get_max_checkpoint_volume_id,
    insert_computer,
    list_all_computers,
    update_computer_status,
)
from mshkn.models import Checkpoint, Computer, Manifest
from mshkn.vm.firecracker import (
    FirecrackerClient,
    FirecrackerConfig,
    kill_firecracker_process,
    start_firecracker_process,
)
from mshkn.vm.network import create_tap, destroy_tap, slot_to_ip, slot_to_mac, slot_to_tap
from mshkn.vm.storage import create_snapshot, remove_volume

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config

logger = logging.getLogger(__name__)

_DEFAULT_MEM_MIB = 512
_DEFAULT_VCPU = 2


def parse_needs(needs: dict[str, object] | None) -> tuple[int, int]:
    """Parse a needs dict into (mem_size_mib, vcpu_count)."""
    if not needs:
        return _DEFAULT_MEM_MIB, _DEFAULT_VCPU

    mem_size_mib = _DEFAULT_MEM_MIB
    vcpu_count = _DEFAULT_VCPU

    ram = needs.get("ram")
    if isinstance(ram, str):
        raw = ram.strip().upper()
        if raw.endswith("GB"):
            mem_size_mib = int(float(raw[:-2]) * 1024)
        elif raw.endswith("MB"):
            mem_size_mib = int(float(raw[:-2]))

    cores = needs.get("cores")
    if isinstance(cores, int):
        vcpu_count = cores
    elif isinstance(cores, str):
        vcpu_count = int(cores)

    return mem_size_mib, vcpu_count


class VMManager:
    def __init__(self, config: Config, db: aiosqlite.Connection) -> None:
        self.config = config
        self.db = db
        self._next_slot = 1  # slot 0 reserved; will be loaded from DB on startup
        self._next_volume_id = 100  # volume 0 is base; start high to avoid conflicts
        self._alloc_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Load state from DB and actual pool to set counters correctly."""
        computers = await list_all_computers(self.db)
        max_vol = 99  # start at 100 by default
        if computers:
            max_vol = max(max_vol, max(c.thin_volume_id for c in computers))
            slots = [int(c.tap_device.replace("tap", "")) for c in computers]
            self._next_slot = max(slots) + 1
        # Also check checkpoint volumes (frozen disk snapshots)
        ckpt_max = await get_max_checkpoint_volume_id(self.db)
        if ckpt_max is not None:
            max_vol = max(max_vol, ckpt_max)
        # Also check capability cache volumes
        from mshkn.capability.cache import get_max_capability_volume_id

        cap_max = await get_max_capability_volume_id(self.db)
        if cap_max is not None:
            max_vol = max(max_vol, cap_max)
        # Scan actual dm-thin pool for orphaned volumes the DB doesn't know about
        pool_max = await self._scan_pool_max_volume_id()
        if pool_max is not None:
            max_vol = max(max_vol, pool_max)
        self._next_volume_id = max_vol + 1
        logger.info(
            "Initialized: next_volume_id=%d, next_slot=%d",
            self._next_volume_id, self._next_slot,
        )

    async def _scan_pool_max_volume_id(self) -> int | None:
        """Scan the dm-thin pool for the highest volume ID in use.

        This catches orphaned volumes that the DB doesn't know about
        (e.g. checkpoint volumes whose DB rows were deleted but whose
        thin volumes were never removed from the pool).
        """
        from mshkn.shell import ShellError, run

        try:
            # dmsetup ls --target thin outputs lines like:
            #   mshkn-base\t(252, 1)
            #   mshkn-comp-abc123\t(252, 5)
            # The number in the table line is the device minor, not the
            # thin volume ID.  To get the actual thin ID we need to parse
            # the table for each device.  But a simpler approach: dmsetup
            # table output for a thin device looks like:
            #   0 <sectors> thin <pool_major:minor> <volume_id>
            output = await run("dmsetup table --target thin")
        except ShellError:
            return None

        max_id = None
        for line in output.strip().splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[3] == "thin":
                try:
                    vol_id = int(parts[5])
                    if max_id is None or vol_id > max_id:
                        max_id = vol_id
                except ValueError:
                    continue
        if max_id is not None:
            logger.info("Pool scan found max volume ID: %d", max_id)
        return max_id

    def _allocate_slot(self) -> int:
        slot = self._next_slot
        self._next_slot += 1
        return slot

    def _allocate_volume_id(self) -> int:
        vol_id = self._next_volume_id
        self._next_volume_id += 1
        return vol_id

    async def _get_or_build_capability_volume(self, manifest: Manifest) -> int:
        """Return the volume_id of a capability base volume for this manifest.

        Checks cache first. On miss, builds the Nix closure and creates
        a new capability base volume. Returns volume 0 (bare base) for empty manifests.
        """
        if not manifest.uses:
            return 0  # bare base image

        manifest_hash = manifest.content_hash()

        # Check cache
        from mshkn.capability.cache import get_cached_volume

        cached_vol = await get_cached_volume(self.db, manifest_hash)
        if cached_vol is not None:
            logger.info("Capability cache hit for %s (vol %d)", manifest_hash, cached_vol)
            return cached_vol

        # Cache miss — build
        logger.info("Capability cache miss for %s, building...", manifest_hash)

        from mshkn.capability.builder import inject_closure_into_volume, nix_build
        from mshkn.capability.resolver import manifest_to_nix

        nix_expr = manifest_to_nix(manifest.uses)
        store_path = await nix_build(nix_expr)

        # Allocate a volume for the capability base
        async with self._alloc_lock:
            cap_volume_id = self._allocate_volume_id()
        cap_volume_name = f"mshkn-cap-{manifest_hash}"

        # Create dm-thin snapshot of base volume
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=0,
            new_volume_id=cap_volume_id,
            new_volume_name=cap_volume_name,
            sectors=self.config.thin_volume_sectors,
        )

        # Inject Nix closure into the volume
        closure_size = await inject_closure_into_volume(
            cap_volume_name,
            store_path,
            manifest.uses,
        )

        # Register in cache
        from mshkn.capability.cache import cache_volume

        await cache_volume(self.db, manifest_hash, cap_volume_id, closure_size)

        logger.info(
            "Built capability volume %s (vol %d, closure %d bytes)",
            manifest_hash,
            cap_volume_id,
            closure_size,
        )
        return cap_volume_id

    async def create(
        self,
        account_id: str,
        manifest: Manifest,
        needs: dict[str, object] | None = None,
    ) -> Computer:
        mem_size_mib, vcpu_count = parse_needs(needs)
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"

        # Get capability base volume (builds if cache miss)
        source_volume_id = await self._get_or_build_capability_volume(manifest)

        async with self._alloc_lock:
            slot = self._allocate_slot()
            volume_id = self._allocate_volume_id()
        _host_ip, vm_ip = slot_to_ip(slot)
        mac = slot_to_mac(slot)
        tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{computer_id}.socket"
        volume_name = f"mshkn-{computer_id}"

        # 1. Create tap device
        await create_tap(slot)

        # 2. Create dm-thin snapshot from capability base
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=source_volume_id,
            new_volume_id=volume_id,
            new_volume_name=volume_name,
            sectors=self.config.thin_volume_sectors,
        )

        # 3. Start Firecracker
        pid = await start_firecracker_process(socket_path)

        # 4. Configure and boot
        fc_client = FirecrackerClient(socket_path)
        try:
            await fc_client.configure_and_boot(
                FirecrackerConfig(
                    socket_path=socket_path,
                    kernel_path=str(self.config.kernel_path),
                    rootfs_path=f"/dev/mapper/{volume_name}",
                    tap_device=tap,
                    guest_mac=mac,
                    vcpu_count=vcpu_count,
                    mem_size_mib=mem_size_mib,
                )
            )
        finally:
            await fc_client.close()

        # 5. Wait for SSH readiness
        await self._wait_for_ssh(vm_ip)

        # 6. Record in DB
        now = datetime.now(UTC).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=tap,
            vm_ip=vm_ip,
            socket_path=socket_path,
            firecracker_pid=pid,
            manifest_hash=manifest.content_hash(),
            manifest_json=manifest.to_json(),
            status="running",
            created_at=now,
            last_exec_at=None,
        )
        await insert_computer(self.db, computer)
        logger.info("Created computer %s (slot=%d, ip=%s)", computer_id, slot, vm_ip)
        return computer

    async def snapshot_disk_for_checkpoint(
        self, computer: Computer, checkpoint_id: str,
    ) -> int:
        """Create a dm-thin CoW snapshot of a computer's disk for checkpoint.

        Returns the new volume ID. The snapshot freezes the disk at this point
        in time so forks get the correct state regardless of what the source
        computer does afterwards.
        """
        async with self._alloc_lock:
            volume_id = self._allocate_volume_id()
        volume_name = f"mshkn-ckpt-{checkpoint_id}"
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=computer.thin_volume_id,
            new_volume_id=volume_id,
            new_volume_name=volume_name,
            sectors=self.config.thin_volume_sectors,
        )
        logger.info(
            "Snapshot disk for checkpoint %s (vol %d from %d)",
            checkpoint_id, volume_id, computer.thin_volume_id,
        )
        return volume_id

    async def fork_from_checkpoint(self, account_id: str, checkpoint: Checkpoint) -> Computer:
        """Fork a new computer from a checkpoint.

        Creates a dm-thin CoW snapshot of the checkpoint's frozen disk (O(1)) and
        cold-boots a new VM from it. The checkpoint's thin_volume_id holds the disk
        state at checkpoint time, so forks always see the correct state.
        """
        if checkpoint.thin_volume_id is None:
            msg = f"Checkpoint {checkpoint.id} has no disk snapshot (created before this fix)"
            raise ValueError(msg)

        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        async with self._alloc_lock:
            slot = self._allocate_slot()
            volume_id = self._allocate_volume_id()
        _host_ip, vm_ip = slot_to_ip(slot)
        mac = slot_to_mac(slot)
        tap = slot_to_tap(slot)
        socket_path = f"/tmp/fc-{computer_id}.socket"
        volume_name = f"mshkn-{computer_id}"

        # 1. Create tap device
        await create_tap(slot)

        # 2. Create dm-thin snapshot from the checkpoint's frozen disk (O(1) CoW)
        await create_snapshot(
            pool_name=self.config.thin_pool_name,
            source_volume_id=checkpoint.thin_volume_id,
            new_volume_id=volume_id,
            new_volume_name=volume_name,
            sectors=self.config.thin_volume_sectors,
        )

        # 3. Start Firecracker and cold-boot from the snapshot disk
        pid = await start_firecracker_process(socket_path)
        fc_client = FirecrackerClient(socket_path)
        try:
            await fc_client.configure_and_boot(
                FirecrackerConfig(
                    socket_path=socket_path,
                    kernel_path=str(self.config.kernel_path),
                    rootfs_path=f"/dev/mapper/{volume_name}",
                    tap_device=tap,
                    guest_mac=mac,
                    vcpu_count=2,
                    mem_size_mib=512,
                )
            )
        finally:
            await fc_client.close()

        # 4. Wait for SSH readiness
        await self._wait_for_ssh(vm_ip)

        # 5. Record in DB
        now = datetime.now(UTC).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=tap,
            vm_ip=vm_ip,
            socket_path=socket_path,
            firecracker_pid=pid,
            manifest_hash=checkpoint.manifest_hash,
            status="running",
            created_at=now,
            last_exec_at=None,
            source_checkpoint_id=checkpoint.id,
        )
        await insert_computer(self.db, computer)
        logger.info(
            "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
            computer_id, checkpoint.id, slot, vm_ip,
        )
        return computer

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise ValueError(f"Computer {computer_id} not found")

        # Kill Firecracker and wait for kernel to release the block device
        if computer.firecracker_pid is not None:
            await kill_firecracker_process(computer.firecracker_pid)
            await asyncio.sleep(0.5)

        # Remove dm-thin volume
        volume_name = f"mshkn-{computer_id}"
        await remove_volume(
            self.config.thin_pool_name, volume_name, computer.thin_volume_id,
        )

        # Remove tap device
        slot = int(computer.tap_device.replace("tap", ""))
        await destroy_tap(slot)

        # Update DB
        await update_computer_status(self.db, computer_id, "destroyed")
        logger.info("Destroyed computer %s", computer_id)

    async def _wait_for_ssh(self, vm_ip: str, timeout: float = 30.0) -> None:
        """Poll SSH until the VM is reachable."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=2",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-i",
                    str(self.config.ssh_key_path),
                    f"root@{vm_ip}",
                    "true",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await proc.wait()
                if rc == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise TimeoutError(f"VM at {vm_ip} did not become reachable via SSH")
