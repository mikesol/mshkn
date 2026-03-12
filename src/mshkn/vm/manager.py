from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import (
    get_computer,
    get_max_checkpoint_volume_id,
    insert_computer,
    list_all_computers,
    update_computer_status,
)
from mshkn.models import Checkpoint, Computer, Manifest
from mshkn.shell import run
from mshkn.vm.firecracker import (
    FirecrackerClient,
    FirecrackerConfig,
    kill_firecracker_process,
    start_firecracker_process,
)
from mshkn.vm.network import create_tap, destroy_tap, slot_to_ip
from mshkn.vm.storage import create_snapshot, pool_create_snap, remove_volume

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.proxy.caddy import CaddyClient
    from mshkn.vm.ssh import SSHPool

logger = logging.getLogger(__name__)

_DEFAULT_MEM_MIB = 256
_ALERT_HISTORY_SIZE = 100


@dataclass
class Alert:
    level: str  # "warning" or "critical"
    source: str  # e.g. "nvme", "ram", "s3"
    message: str
    value: float  # the metric value that triggered it
    threshold: float  # the threshold that was exceeded
    timestamp: str  # ISO 8601
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
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        caddy: CaddyClient | None = None,
        ssh_pool: SSHPool | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.caddy = caddy
        self.ssh_pool = ssh_pool
        self._next_slot = 1  # slot 0 reserved; will be loaded from DB on startup
        self._free_slots: list[int] = []  # recycled slots from destroyed VMs
        self._next_volume_id = 100  # volume 0 is base; start high to avoid conflicts
        self._alloc_lock = asyncio.Lock()
        self.alerts: deque[Alert] = deque(maxlen=_ALERT_HISTORY_SIZE)
        self._bg_tasks: set[asyncio.Task[None]] = set()

    async def initialize(self) -> None:
        """Load state from DB and actual pool to set counters correctly."""
        computers = await list_all_computers(self.db)
        max_vol = 99  # start at 100 by default
        if computers:
            max_vol = max(max_vol, max(c.thin_volume_id for c in computers))
            running = [c for c in computers if c.status == "running"]
            if running:
                active_slots = {int(c.tap_device.replace("tap", "")) for c in running}
                self._next_slot = min(max(active_slots) + 1, 256)
                # Recycle any gaps in the slot range
                for s in range(1, self._next_slot):
                    if s not in active_slots:
                        self._free_slots.append(s)
            else:
                self._next_slot = 1
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
        if self._free_slots:
            slot = self._free_slots.pop()
            if slot == 254:  # staging slot, skip
                return self._allocate_slot()
            return slot
        slot = self._next_slot
        if slot == 254:  # skip staging slot
            self._next_slot = 255
            slot = 255
        if slot > 255:
            raise RuntimeError("No free VM slots (all 255 in use)")
        self._next_slot += 1
        return slot

    def _release_slot(self, slot: int) -> None:
        self._free_slots.append(slot)

    def _allocate_volume_id(self) -> int:
        vol_id = self._next_volume_id
        self._next_volume_id += 1
        return vol_id

    async def _start_firecracker_with_snapshot(
        self,
        source_volume_id: int,
        volume_id: int,
        volume_name: str,
        socket_path: str,
    ) -> int:
        snapshot_task = asyncio.create_task(
            create_snapshot(
                pool_name=self.config.thin_pool_name,
                source_volume_id=source_volume_id,
                new_volume_id=volume_id,
                new_volume_name=volume_name,
                sectors=self.config.thin_volume_sectors,
            )
        )
        try:
            pid = await start_firecracker_process(socket_path)
        except Exception:
            snapshot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshot_task
            raise

        try:
            await snapshot_task
        except Exception:
            await kill_firecracker_process(pid)
            raise

        return pid

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

        # Cache miss — evict stale volumes if disk is tight, then build
        from mshkn.capability.eviction import evict_lru_capabilities

        evicted = await evict_lru_capabilities(self.db, self.config.thin_pool_name)
        if evicted:
            logger.info("Evicted %d capability volumes before build", evicted)

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

    async def _build_l3_template(
        self, manifest_hash: str, source_volume_id: int
    ) -> None:
        """Build an L3 template: cold-boot on staging slot, snapshot, cache."""
        from mshkn.capability.template_cache import cache_template
        from mshkn.vm.staging import (
            STAGING_DRIVE_NAME,
            STAGING_MAC,
            STAGING_SLOT,
            STAGING_TAP,
            STAGING_VM_IP,
            _restore_lock,
        )

        template_dir = self.config.checkpoint_local_dir / "templates" / manifest_hash
        template_dir.mkdir(parents=True, exist_ok=True)
        vmstate_path = template_dir / "vmstate"
        memory_path = template_dir / "memory"

        socket_path = f"/tmp/fc-template-{manifest_hash}.socket"
        pid: int | None = None

        async with _restore_lock:
            try:
                from mshkn.vm.staging import _ensure_staging_clean

                await _ensure_staging_clean()

                # Map capability volume as staging drive + create tap in parallel
                await asyncio.gather(
                    run(
                        f"dmsetup create {STAGING_DRIVE_NAME} "
                        f"--table '0 {self.config.thin_volume_sectors} thin "
                        f"/dev/mapper/{self.config.thin_pool_name} "
                        f"{source_volume_id}'"
                    ),
                    create_tap(STAGING_SLOT),
                )

                # Start FC and cold-boot on staging slot
                pid = await start_firecracker_process(socket_path)
                fc_client = FirecrackerClient(socket_path)
                try:
                    await fc_client.configure_and_boot(
                        FirecrackerConfig(
                            socket_path=socket_path,
                            kernel_path=str(self.config.kernel_path),
                            rootfs_path=f"/dev/mapper/{STAGING_DRIVE_NAME}",
                            tap_device=STAGING_TAP,
                            guest_mac=STAGING_MAC,
                        )
                    )
                finally:
                    await fc_client.close()

                # Wait for SSH
                await self._wait_for_ssh(STAGING_VM_IP)

                # Pause and snapshot
                fc_client = FirecrackerClient(socket_path)
                try:
                    await fc_client.pause()
                    await fc_client.create_snapshot(str(vmstate_path), str(memory_path))
                finally:
                    await fc_client.close()

                # Kill template VM
                await kill_firecracker_process(pid)
                pid = None

                # Destroy staging tap
                await destroy_tap(STAGING_SLOT)

                # Remove staging drive mapping
                await run(f"dmsetup remove {STAGING_DRIVE_NAME}")

                # Cache the template
                await cache_template(
                    self.db, manifest_hash, str(vmstate_path), str(memory_path)
                )
                logger.info("Built L3 template for %s", manifest_hash)

            except Exception:
                logger.exception("Failed to build L3 template for %s", manifest_hash)
                # Cleanup
                if pid is not None:
                    await kill_firecracker_process(pid)
                await destroy_tap(STAGING_SLOT)
                await run(f"dmsetup remove {STAGING_DRIVE_NAME}", check=False)
                raise

    async def create(
        self,
        account_id: str,
        manifest: Manifest,
        needs: dict[str, object] | None = None,
    ) -> Computer:
        mem_size_mib, vcpu_count = parse_needs(needs)
        custom_resources = (
            mem_size_mib != _DEFAULT_MEM_MIB or vcpu_count != _DEFAULT_VCPU
        )
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"

        # Get capability base volume (L1/L2 cache, builds if miss)
        source_volume_id = await self._get_or_build_capability_volume(manifest)
        manifest_hash = manifest.content_hash() if manifest.uses else "bare"

        # Allocate slot + volume
        async with self._alloc_lock:
            slot = self._allocate_slot()
            volume_id = self._allocate_volume_id()
        volume_name = f"mshkn-{computer_id}"

        # Create dm-thin snapshot in pool (no device activation — staging will activate it)
        await pool_create_snap(self.config.thin_pool_name, volume_id, source_volume_id)

        if custom_resources:
            # Custom RAM/vCPU: cold-boot directly (L3 templates bake in default config)
            from mshkn.vm.staging import cold_boot_from_disk

            logger.info(
                "Cold-booting with custom resources: mem=%dMiB, vcpu=%d",
                mem_size_mib, vcpu_count,
            )
            result = await cold_boot_from_disk(
                disk_volume_id=volume_id,
                final_slot=slot,
                pool_name=self.config.thin_pool_name,
                thin_volume_sectors=self.config.thin_volume_sectors,
                final_volume_name=volume_name,
                kernel_path=str(self.config.kernel_path),
                mem_size_mib=mem_size_mib,
                vcpu_count=vcpu_count,
                socket_path=f"/tmp/fc-{computer_id}.socket",
            )
        else:
            # Default resources: use L3 template cache for fast restore
            from mshkn.capability.template_cache import get_cached_template

            template = await get_cached_template(self.db, manifest_hash)
            if template is None:
                logger.info("L3 cache miss for %s, building template...", manifest_hash)
                await self._build_l3_template(manifest_hash, source_volume_id)
                template = await get_cached_template(self.db, manifest_hash)
                if template is None:
                    raise RuntimeError(f"L3 template build failed for {manifest_hash}")

            vmstate_path, memory_path = template

            from mshkn.vm.staging import restore_from_snapshot

            result = await restore_from_snapshot(
                vmstate_path=vmstate_path,
                memory_path=memory_path,
                disk_volume_id=volume_id,
                final_slot=slot,
                pool_name=self.config.thin_pool_name,
                thin_volume_sectors=self.config.thin_volume_sectors,
                final_volume_name=volume_name,
                socket_path=f"/tmp/fc-{computer_id}.socket",
            )

        # Warm SSH pool
        _host_ip, vm_ip = slot_to_ip(slot)
        if self.ssh_pool is not None:
            await self.ssh_pool.get(vm_ip)

        # Record in DB
        now = datetime.now(UTC).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=result.tap_device,
            vm_ip=result.vm_ip,
            socket_path=result.socket_path,
            firecracker_pid=result.pid,
            manifest_hash=manifest.content_hash(),
            manifest_json=manifest.to_json(),
            status="running",
            created_at=now,
            last_exec_at=None,
        )
        await insert_computer(self.db, computer)

        # Register Caddy route
        if self.caddy is not None:
            await self.caddy.add_route(computer_id, vm_ip)

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

    async def fork_from_checkpoint(
        self, account_id: str, checkpoint: Checkpoint, manifest: Manifest | None = None,
    ) -> Computer:
        """Fork a new computer from a checkpoint via LOAD_SNAPSHOT.

        All checkpoints are staging-compatible because all VMs are created
        via the staging slot (two-phase boot ensures this).
        """
        if checkpoint.thin_volume_id is None:
            msg = f"Checkpoint {checkpoint.id} has no disk snapshot (created before this fix)"
            raise ValueError(msg)

        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        async with self._alloc_lock:
            slot = self._allocate_slot()
            volume_id = self._allocate_volume_id()
        volume_name = f"mshkn-{computer_id}"

        # Create dm-thin snapshot of checkpoint's disk (pool only, no device activation)
        await pool_create_snap(self.config.thin_pool_name, volume_id, checkpoint.thin_volume_id)

        # Check if checkpoint has vmstate/memory (merge checkpoints don't)
        ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
        vmstate_path = str(ckpt_dir / "vmstate")
        memory_path = str(ckpt_dir / "memory")
        has_snapshot = Path(vmstate_path).exists() and Path(memory_path).exists()

        if not has_snapshot:
            # Try downloading from R2
            try:
                await self._download_checkpoint_snapshot(checkpoint)
                has_snapshot = Path(vmstate_path).exists() and Path(memory_path).exists()
            except Exception:
                logger.info(
                    "No snapshot files for checkpoint %s, will cold-boot",
                    checkpoint.id,
                )

        if has_snapshot:
            # Standard path: restore from snapshot via staging slot
            from mshkn.vm.staging import restore_from_snapshot

            result = await restore_from_snapshot(
                vmstate_path=vmstate_path,
                memory_path=memory_path,
                disk_volume_id=volume_id,
                final_slot=slot,
                pool_name=self.config.thin_pool_name,
                thin_volume_sectors=self.config.thin_volume_sectors,
                final_volume_name=volume_name,
                socket_path=f"/tmp/fc-{computer_id}.socket",
            )
        else:
            # Merge checkpoint (no vmstate/memory): cold-boot from disk
            from mshkn.vm.staging import cold_boot_from_disk

            logger.info(
                "Cold-booting fork from merge checkpoint %s", checkpoint.id,
            )
            result = await cold_boot_from_disk(
                disk_volume_id=volume_id,
                final_slot=slot,
                pool_name=self.config.thin_pool_name,
                thin_volume_sectors=self.config.thin_volume_sectors,
                final_volume_name=volume_name,
                kernel_path=str(self.config.kernel_path),
                socket_path=f"/tmp/fc-{computer_id}.socket",
            )

        # Warm SSH pool
        if self.ssh_pool is not None:
            await self.ssh_pool.get(result.vm_ip)

        # Record in DB
        now = datetime.now(UTC).isoformat()
        effective_manifest = manifest if manifest is not None else Manifest.from_json(
            checkpoint.manifest_json,
        )
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=result.tap_device,
            vm_ip=result.vm_ip,
            socket_path=result.socket_path,
            firecracker_pid=result.pid,
            manifest_hash=effective_manifest.content_hash(),
            manifest_json=effective_manifest.to_json(),
            status="running",
            created_at=now,
            last_exec_at=None,
            source_checkpoint_id=checkpoint.id,
        )
        await insert_computer(self.db, computer)

        # Register Caddy route
        if self.caddy is not None:
            await self.caddy.add_route(computer_id, result.vm_ip)

        logger.info(
            "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
            computer_id, checkpoint.id, slot, result.vm_ip,
        )
        return computer

    async def _download_checkpoint_snapshot(self, checkpoint: Checkpoint) -> None:
        """Download vmstate + memory files from R2 if not cached locally."""
        ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
        ckpt_dir.mkdir(parents=True, exist_ok=True)

        if not checkpoint.r2_prefix:
            raise ValueError(f"Checkpoint {checkpoint.id} has no R2 prefix")

        for filename in ("vmstate", "memory"):
            local_path = ckpt_dir / filename
            if not local_path.exists():
                r2_path = f"{self.config.r2_bucket}:{checkpoint.r2_prefix}/{filename}"
                await run(f"rclone copyto r2:{r2_path} {local_path}")
                logger.info("Downloaded %s for checkpoint %s", filename, checkpoint.id)

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise ValueError(f"Computer {computer_id} not found")

        # Remove Caddy route first (so traffic stops immediately)
        if self.caddy is not None:
            await self.caddy.remove_route(computer_id)

        # Kill Firecracker (kill_firecracker_process waits for exit)
        if computer.firecracker_pid is not None:
            await kill_firecracker_process(computer.firecracker_pid)

        # Remove dm-thin volume
        volume_name = f"mshkn-{computer_id}"
        await remove_volume(
            self.config.thin_pool_name, volume_name, computer.thin_volume_id,
        )

        # Remove tap device and recycle slot
        slot = int(computer.tap_device.replace("tap", ""))
        await destroy_tap(slot)
        async with self._alloc_lock:
            self._release_slot(slot)

        # Clean up SSH pool connection
        if self.ssh_pool is not None and computer.vm_ip:
            await self.ssh_pool.remove(computer.vm_ip)

        # Update DB
        await update_computer_status(self.db, computer_id, "destroyed")
        logger.info("Destroyed computer %s", computer_id)

    # ── Stale VM Reaper ───────────────────────────────────────────────────

    def _is_pid_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but we can't signal it

    async def reap_dead_vms(self) -> int:
        """Find VMs whose Firecracker process has died and clean them up.

        Returns the number of VMs reaped.
        """
        computers = await list_all_computers(self.db)
        running = [c for c in computers if c.status == "running"]
        reaped = 0

        for computer in running:
            if computer.firecracker_pid is None:
                continue
            if self._is_pid_alive(computer.firecracker_pid):
                continue

            logger.warning(
                "Reaping dead VM %s (PID %d no longer running)",
                computer.id, computer.firecracker_pid,
            )
            try:
                await self._cleanup_dead_vm(computer)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap VM %s", computer.id)

        return reaped

    async def _cleanup_dead_vm(self, computer: Computer) -> None:
        """Clean up resources for a VM whose process is already dead."""
        # Remove Caddy route
        if self.caddy is not None:
            try:
                await self.caddy.remove_route(computer.id)
            except Exception:
                logger.debug("Caddy route removal failed for %s (may not exist)", computer.id)

        # Remove dm-thin volume (process is already dead, no need to wait)
        volume_name = f"mshkn-{computer.id}"
        try:
            await remove_volume(
                self.config.thin_pool_name, volume_name, computer.thin_volume_id,
            )
        except Exception:
            logger.debug("Volume removal failed for %s (may already be gone)", computer.id)

        # Remove tap device and recycle slot
        slot = int(computer.tap_device.replace("tap", ""))
        try:
            await destroy_tap(slot)
        except Exception:
            logger.debug("TAP removal failed for %s (may already be gone)", computer.id)
        async with self._alloc_lock:
            self._release_slot(slot)

        # Mark destroyed in DB
        await update_computer_status(self.db, computer.id, "destroyed")
        logger.info("Reaped dead VM %s", computer.id)

    async def reap_idle_vms(self) -> int:
        """Find VMs that have been idle beyond the timeout and auto-checkpoint + destroy.

        Returns the number of VMs reaped.
        """
        if self.config.idle_timeout_seconds <= 0:
            return 0

        computers = await list_all_computers(self.db)
        running = [c for c in computers if c.status == "running"]
        now = datetime.now(UTC)

        idle_vms: list[Computer] = []
        for computer in running:
            # Use last_exec_at if available, otherwise created_at
            ref_time_str = computer.last_exec_at or computer.created_at
            try:
                ref_time = datetime.fromisoformat(ref_time_str)
                if ref_time.tzinfo is None:
                    ref_time = ref_time.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue

            idle_seconds = (now - ref_time).total_seconds()
            if idle_seconds >= self.config.idle_timeout_seconds:
                logger.info(
                    "Auto-checkpointing idle VM %s (idle %.0fs, timeout %ds)",
                    computer.id, idle_seconds, self.config.idle_timeout_seconds,
                )
                idle_vms.append(computer)

        if not idle_vms:
            return 0

        # Process idle VMs concurrently (up to 5 at a time)
        sem = asyncio.Semaphore(5)

        async def _process(comp: Computer) -> bool:
            async with sem:
                try:
                    await self._auto_checkpoint_and_destroy(comp)
                    return True
                except Exception:
                    logger.exception("Failed to auto-checkpoint idle VM %s", comp.id)
                    return False

        results = await asyncio.gather(*[_process(c) for c in idle_vms])
        return sum(1 for r in results if r)

    async def _auto_checkpoint_and_destroy(self, computer: Computer) -> None:
        """Auto-checkpoint a VM and then destroy it."""
        import uuid as _uuid

        from mshkn.checkpoint.r2 import upload_checkpoint
        from mshkn.checkpoint.snapshot import create_vm_snapshot
        from mshkn.db import (
            get_latest_checkpoint_for_computer,
            insert_checkpoint,
        )
        from mshkn.models import Checkpoint
        from mshkn.vm.ssh import ssh_exec

        checkpoint_id = f"ckpt-{_uuid.uuid4().hex[:12]}"
        snapshot_dir = self.config.checkpoint_local_dir / checkpoint_id

        try:
            # Flush guest filesystem
            await ssh_exec(
                computer.vm_ip, "sync", self.config.ssh_key_path,
                timeout=10.0, pool=self.ssh_pool,
            )

            # Pause/snapshot/resume
            await create_vm_snapshot(computer.socket_path, snapshot_dir)

            # Evict SSH pool connection — pause/resume disrupts TCP session
            if self.ssh_pool is not None and computer.vm_ip:
                await self.ssh_pool.remove(computer.vm_ip)

            # Freeze disk
            ckpt_volume_id = await self.snapshot_disk_for_checkpoint(
                computer, checkpoint_id,
            )

            # Determine parent
            latest = await get_latest_checkpoint_for_computer(self.db, computer.id)
            if latest is not None:
                parent_id = latest.id
            elif computer.source_checkpoint_id is not None:
                parent_id = computer.source_checkpoint_id
            else:
                parent_id = None

            now = datetime.now(UTC).isoformat()
            r2_prefix = f"{computer.account_id}/{checkpoint_id}"
            ckpt = Checkpoint(
                id=checkpoint_id,
                account_id=computer.account_id,
                parent_id=parent_id,
                computer_id=computer.id,
                thin_volume_id=ckpt_volume_id,
                manifest_hash=computer.manifest_hash,
                manifest_json=computer.manifest_json,
                r2_prefix=r2_prefix,
                disk_delta_size_bytes=0,
                memory_size_bytes=0,
                label="auto-idle-timeout",
                pinned=False,
                created_at=now,
            )
            await insert_checkpoint(self.db, ckpt)

            # Upload to R2 in background (best-effort, don't block reaper)
            task = asyncio.create_task(
                self._upload_checkpoint_bg(
                    upload_checkpoint, snapshot_dir, r2_prefix,
                    self.config.r2_bucket, checkpoint_id,
                )
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

            logger.info("Auto-checkpoint %s created for idle VM %s", checkpoint_id, computer.id)
        except Exception:
            logger.exception("Auto-checkpoint failed for VM %s, destroying anyway", computer.id)

        # Destroy the VM
        await self.destroy(computer.id)
        logger.info("Destroyed idle VM %s", computer.id)

    @staticmethod
    async def _upload_checkpoint_bg(
        upload_fn: object,
        snapshot_dir: Path,
        r2_prefix: str,
        bucket: str,
        checkpoint_id: str,
    ) -> None:
        """Background R2 upload for auto-checkpoints."""
        try:
            await upload_fn(snapshot_dir, r2_prefix, bucket)  # type: ignore[operator]
        except Exception:
            logger.warning(
                "R2 upload failed for auto-checkpoint %s", checkpoint_id,
            )

    async def prune_checkpoints(self) -> int:
        """Delete checkpoints that exceed the per-account retention count.

        Pinned checkpoints are never deleted. Returns total pruned count.
        """
        from mshkn.checkpoint.r2 import delete_checkpoint_r2
        from mshkn.db import (
            delete_checkpoint,
            list_account_ids_with_checkpoints,
            list_prunable_checkpoints,
        )
        from mshkn.vm.storage import remove_volume

        keep = self.config.checkpoint_retention_count
        if keep <= 0:
            return 0

        account_ids = await list_account_ids_with_checkpoints(self.db)
        pruned = 0

        for account_id in account_ids:
            excess = await list_prunable_checkpoints(self.db, account_id, keep)
            for ckpt in excess:
                logger.info(
                    "Pruning checkpoint %s (account=%s, created=%s)",
                    ckpt.id, account_id, ckpt.created_at,
                )
                try:
                    # Remove dm-thin volume
                    if ckpt.thin_volume_id is not None:
                        vol_name = f"mshkn-ckpt-{ckpt.id}"
                        try:
                            await remove_volume(
                                self.config.thin_pool_name,
                                vol_name,
                                ckpt.thin_volume_id,
                            )
                        except Exception:
                            logger.debug(
                                "Volume removal failed for ckpt %s (may be gone)",
                                ckpt.id,
                            )

                    # Remove local snapshot files
                    import shutil

                    local_dir = self.config.checkpoint_local_dir / ckpt.id
                    if local_dir.exists():
                        shutil.rmtree(local_dir)

                    # Remove from R2
                    try:
                        await delete_checkpoint_r2(
                            ckpt.r2_prefix, self.config.r2_bucket,
                        )
                    except Exception:
                        logger.debug("R2 cleanup failed for ckpt %s", ckpt.id)

                    # Delete DB record
                    await delete_checkpoint(self.db, ckpt.id)
                    pruned += 1
                except Exception:
                    logger.exception("Failed to prune checkpoint %s", ckpt.id)

        return pruned

    async def check_host_resources(self) -> list[Alert]:
        """Check host-level resource usage and return any new alerts."""
        now = datetime.now(UTC).isoformat()
        new_alerts: list[Alert] = []

        # Check NVMe disk usage
        try:
            disk = shutil.disk_usage("/")
            pct = (disk.used / disk.total) * 100
            if pct > 80:
                level = "critical" if pct > 95 else "warning"
                alert = Alert(
                    level=level,
                    source="nvme",
                    message=f"NVMe usage at {pct:.1f}%",
                    value=round(pct, 1),
                    threshold=80.0,
                    timestamp=now,
                )
                new_alerts.append(alert)
                logger.warning("ALERT [%s]: %s", level, alert.message)
        except Exception:
            logger.exception("Failed to check disk usage")

        # Check host RAM usage
        try:
            with Path("/proc/meminfo").open() as f:
                meminfo: dict[str, int] = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(":")
                        meminfo[key] = int(parts[1])  # kB
            total = meminfo.get("MemTotal", 0)
            available = meminfo.get("MemAvailable", 0)
            if total > 0:
                used_pct = ((total - available) / total) * 100
                if used_pct > 90:
                    alert = Alert(
                        level="critical",
                        source="ram",
                        message=f"Host RAM usage at {used_pct:.1f}%",
                        value=round(used_pct, 1),
                        threshold=90.0,
                        timestamp=now,
                    )
                    new_alerts.append(alert)
                    logger.warning("ALERT [critical]: %s", alert.message)
        except Exception:
            logger.exception("Failed to check RAM usage")

        for alert in new_alerts:
            self.alerts.append(alert)
        return new_alerts

    async def run_reaper_loop(self, interval: float = 60.0) -> None:
        """Background loop that reaps dead VMs, idle VMs, and excess checkpoints."""
        idle_timeout = self.config.idle_timeout_seconds
        retention = self.config.checkpoint_retention_count
        logger.info(
            "Reaper started (interval=%.0fs, idle_timeout=%ds, retention=%d)",
            interval, idle_timeout, retention,
        )
        while True:
            await asyncio.sleep(interval)
            try:
                dead = await self.reap_dead_vms()
                idle = await self.reap_idle_vms()
                pruned = await self.prune_checkpoints()
                host_alerts = await self.check_host_resources()
                if dead or idle or pruned or host_alerts:
                    logger.info(
                        "Reaper cycle: %d dead, %d idle VM(s), "
                        "%d checkpoint(s) pruned, %d alert(s)",
                        dead, idle, pruned, len(host_alerts),
                    )
            except Exception:
                logger.exception("Reaper cycle failed")

    async def _wait_for_ssh(self, vm_ip: str, timeout: float = 30.0) -> None:
        """Poll until VM port 22 accepts TCP connections."""
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
            await asyncio.sleep(0.025)
        raise TimeoutError(f"VM at {vm_ip} did not become reachable on port 22")
