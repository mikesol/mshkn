from __future__ import annotations

import asyncio
import logging
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
from mshkn.errors import Conflict, NotFound
from mshkn.host import SnapshotFiles
from mshkn.models import Checkpoint, Computer, ComputerStatus, Recipe, RecipeStatus
from mshkn.observability.metrics import checkpoints_total, host_ram_used_ratio
from mshkn.resources import DEFAULT_RESOURCES, Resources

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.runtime import BackgroundTasks

logger = logging.getLogger(__name__)

_ALERT_HISTORY_SIZE = 100


@dataclass
class Alert:
    level: str  # "warning" or "critical"
    source: str  # e.g. "nvme", "ram", "s3"
    message: str
    value: float  # the metric value that triggered it
    threshold: float  # the threshold that was exceeded
    timestamp: str  # ISO 8601


class VMManager:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        *,
        host: Host,
        tasks: BackgroundTasks | None = None,
    ) -> None:
        # runtime imports vm.manager; import here to avoid a cycle until PR 4
        from mshkn.runtime import BackgroundTasks as _BackgroundTasks

        self.config = config
        self.db = db
        self.host = host
        self._next_slot = 1  # slot 0 reserved; will be loaded from DB on startup
        self._free_slots: set[int] = set()  # recycled slots from destroyed VMs
        self._next_volume_id = 100  # volume 0 is base; start high to avoid conflicts
        self._alloc_lock = asyncio.Lock()
        self.alerts: deque[Alert] = deque(maxlen=_ALERT_HISTORY_SIZE)
        self.tasks = tasks if tasks is not None else _BackgroundTasks()

    async def initialize(self) -> None:
        """Load state from DB and actual pool to set counters correctly."""
        computers = await list_all_computers(self.db)
        max_vol = 99  # start at 100 by default
        if computers:
            max_vol = max(max_vol, max(c.thin_volume_id for c in computers))
            running = [c for c in computers if c.status == ComputerStatus.RUNNING]
            if running:
                active_slots = {int(c.tap_device.replace("tap", "")) for c in running}
                self._next_slot = min(max(active_slots) + 1, 256)
                # Recycle any gaps in the slot range
                for s in range(1, self._next_slot):
                    if s not in active_slots:
                        self._free_slots.add(s)
            else:
                self._next_slot = 1
        # Also check checkpoint volumes (frozen disk snapshots)
        ckpt_max = await get_max_checkpoint_volume_id(self.db)
        if ckpt_max is not None:
            max_vol = max(max_vol, ckpt_max)
        # Also check recipe base volumes
        from mshkn.db import get_max_recipe_volume_id

        recipe_max = await get_max_recipe_volume_id(self.db)
        if recipe_max is not None:
            max_vol = max(max_vol, recipe_max)
        # Scan actual dm-thin pool for orphaned volumes the DB doesn't know about
        pool_max = await self.host.blocks.max_volume_id()
        if pool_max is not None:
            max_vol = max(max_vol, pool_max)
        self._next_volume_id = max_vol + 1
        logger.info(
            "Initialized: next_volume_id=%d, next_slot=%d",
            self._next_volume_id,
            self._next_slot,
        )

    def _allocate_slot(self) -> int:
        self._free_slots.discard(254)  # staging slot, never allocate
        if self._free_slots:
            return self._free_slots.pop()
        slot = self._next_slot
        if slot == 254:  # skip staging slot
            self._next_slot = 255
            slot = 255
        if slot > 255:
            raise RuntimeError("No free VM slots (all 255 in use)")
        self._next_slot += 1
        return slot

    def _release_slot(self, slot: int) -> None:
        self._free_slots.add(slot)

    def _allocate_volume_id(self) -> int:
        vol_id = self._next_volume_id
        self._next_volume_id += 1
        return vol_id

    async def _ensure_template(self, recipe: Recipe | None) -> SnapshotFiles | None:
        """Return the L3 template for a recipe (or the bare base), building it once.

        A cached template is returned straight from the DB. Otherwise the disk is
        cold-booted on the staging slot, snapshotted, and the paths cached. A build
        failure is not fatal: it logs a warning and returns None so the caller
        cold-boots instead.
        """
        from mshkn.db import cache_bare_template, get_bare_template, update_recipe_template

        if recipe is not None:
            if recipe.template_vmstate and recipe.template_memory:
                return SnapshotFiles(
                    vmstate=Path(recipe.template_vmstate),
                    memory=Path(recipe.template_memory),
                )
            source_volume_id = recipe.base_volume_id or 0
            dest_dir = self.config.checkpoint_local_dir / "templates" / recipe.id
        else:
            bare = await get_bare_template(self.db)
            if bare is not None:
                return SnapshotFiles(vmstate=Path(bare[0]), memory=Path(bare[1]))
            source_volume_id = 0
            dest_dir = self.config.checkpoint_local_dir / "templates" / "bare"

        try:
            files = await self.host.hypervisor.build_template(
                disk_volume_id=source_volume_id,
                dest_dir=dest_dir,
            )
        except Exception:
            logger.warning(
                "L3 template build failed for %s, will cold-boot",
                recipe.id if recipe is not None else "bare",
            )
            return None

        if recipe is not None:
            await update_recipe_template(self.db, recipe.id, str(files.vmstate), str(files.memory))
            logger.info("Built L3 template for recipe %s", recipe.id)
        else:
            await cache_bare_template(self.db, str(files.vmstate), str(files.memory))
            logger.info("Built bare L3 template")
        return files

    async def create(
        self,
        account_id: str,
        recipe_id: str | None = None,
        resources: Resources = DEFAULT_RESOURCES,
    ) -> Computer:
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"

        # Resolve recipe to source volume
        if recipe_id is not None:
            from mshkn.db import get_recipe

            recipe = await get_recipe(self.db, recipe_id)
            if recipe is None:
                raise NotFound(f"Recipe {recipe_id} not found")
            if recipe.status != RecipeStatus.READY:
                raise Conflict(f"Recipe {recipe_id} is not ready (status={recipe.status})")
            if recipe.base_volume_id is None:
                raise Conflict(f"Recipe {recipe_id} has no base volume")
            source_volume_id = recipe.base_volume_id
        else:
            recipe = None
            source_volume_id = 0  # bare base image

        # Allocate slot + volume
        async with self._alloc_lock:
            slot = self._allocate_slot()
            volume_id = self._allocate_volume_id()
        volume_name = f"mshkn-{computer_id}"

        # Create dm-thin snapshot in pool (no device activation — staging will activate it)
        await self.host.blocks.snap(source_volume_id=source_volume_id, new_volume_id=volume_id)

        # Default resources: use the L3 template cache for a fast restore. Custom
        # RAM/vCPU cold-boots instead, because templates bake in the default config.
        files = await self._ensure_template(recipe) if resources.is_default else None

        if files is not None:
            vm = await self.host.hypervisor.restore(
                slot=slot,
                disk_volume_id=volume_id,
                disk_name=volume_name,
                snapshot=files,
            )
        else:
            if not resources.is_default:
                logger.info(
                    "Cold-booting with custom resources: mem=%dMiB, vcpu=%d",
                    resources.mem_mib,
                    resources.vcpus,
                )
            vm = await self.host.hypervisor.boot(
                slot=slot,
                disk_volume_id=volume_id,
                disk_name=volume_name,
                resources=resources,
            )

        # Warm SSH pool
        await self.host.guest.warm(vm.vm_ip)

        # Record in DB
        now = datetime.now(UTC).isoformat()
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=vm.tap_device,
            vm_ip=vm.vm_ip,
            socket_path=vm.socket_path,
            firecracker_pid=vm.pid,
            status=ComputerStatus.RUNNING,
            created_at=now,
            last_exec_at=None,
            recipe_id=recipe_id,
        )
        await insert_computer(self.db, computer)

        # Register Caddy route
        await self.host.proxy.add_route(computer_id, vm.vm_ip)

        logger.info("Created computer %s (slot=%d, ip=%s)", computer_id, vm.slot, vm.vm_ip)
        return computer

    async def snapshot_disk_for_checkpoint(
        self,
        computer: Computer,
        checkpoint_id: str,
    ) -> int:
        """Create a dm-thin CoW snapshot of a computer's disk for checkpoint.

        Returns the new volume ID. The snapshot freezes the disk at this point
        in time so forks get the correct state regardless of what the source
        computer does afterwards.
        """
        async with self._alloc_lock:
            volume_id = self._allocate_volume_id()
        volume_name = f"mshkn-ckpt-{checkpoint_id}"
        await self.host.blocks.snap(
            source_volume_id=computer.thin_volume_id,
            new_volume_id=volume_id,
        )
        await self.host.blocks.activate(volume_id=volume_id, name=volume_name)
        logger.info(
            "Snapshot disk for checkpoint %s (vol %d from %d)",
            checkpoint_id,
            volume_id,
            computer.thin_volume_id,
        )
        return volume_id

    async def fork_from_checkpoint(
        self,
        account_id: str,
        checkpoint: Checkpoint,
        recipe_id: str | None = None,
    ) -> Computer:
        """Fork a new computer from a checkpoint via LOAD_SNAPSHOT.

        All checkpoints are staging-compatible because all VMs are created
        via the staging slot (two-phase boot ensures this).
        """
        if checkpoint.thin_volume_id is None:
            msg = f"Checkpoint {checkpoint.id} has no disk snapshot (created before this fix)"
            raise Conflict(msg)

        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        async with self._alloc_lock:
            slot = self._allocate_slot()
            volume_id = self._allocate_volume_id()
        volume_name = f"mshkn-{computer_id}"

        # Create dm-thin snapshot of checkpoint's disk (pool only, no device activation)
        await self.host.blocks.snap(
            source_volume_id=checkpoint.thin_volume_id,
            new_volume_id=volume_id,
        )

        # Check if checkpoint has vmstate/memory (merge checkpoints don't)
        ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
        files = SnapshotFiles(vmstate=ckpt_dir / "vmstate", memory=ckpt_dir / "memory")
        has_snapshot = files.vmstate.exists() and files.memory.exists()

        if not has_snapshot:
            # Try downloading from R2
            try:
                await self._download_checkpoint_snapshot(checkpoint)
                has_snapshot = files.vmstate.exists() and files.memory.exists()
            except Exception:
                logger.info(
                    "No snapshot files for checkpoint %s, will cold-boot",
                    checkpoint.id,
                )

        if has_snapshot:
            # Standard path: restore from snapshot via staging slot
            vm = await self.host.hypervisor.restore(
                slot=slot,
                disk_volume_id=volume_id,
                disk_name=volume_name,
                snapshot=files,
            )
        else:
            # Merge checkpoint (no vmstate/memory): cold-boot from disk
            logger.info(
                "Cold-booting fork from merge checkpoint %s",
                checkpoint.id,
            )
            vm = await self.host.hypervisor.boot(
                slot=slot,
                disk_volume_id=volume_id,
                disk_name=volume_name,
                resources=DEFAULT_RESOURCES,
            )

        # Warm SSH pool
        await self.host.guest.warm(vm.vm_ip)

        # Record in DB
        now = datetime.now(UTC).isoformat()
        effective_recipe_id = recipe_id if recipe_id is not None else checkpoint.recipe_id
        computer = Computer(
            id=computer_id,
            account_id=account_id,
            thin_volume_id=volume_id,
            tap_device=vm.tap_device,
            vm_ip=vm.vm_ip,
            socket_path=vm.socket_path,
            firecracker_pid=vm.pid,
            status=ComputerStatus.RUNNING,
            created_at=now,
            last_exec_at=None,
            source_checkpoint_id=checkpoint.id,
            recipe_id=effective_recipe_id,
        )
        await insert_computer(self.db, computer)

        # Register Caddy route
        await self.host.proxy.add_route(computer_id, vm.vm_ip)

        logger.info(
            "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
            computer_id,
            checkpoint.id,
            vm.slot,
            vm.vm_ip,
        )
        return computer

    async def _download_checkpoint_snapshot(self, checkpoint: Checkpoint) -> None:
        """Download the checkpoint's snapshot files from R2 if not cached locally."""
        if not checkpoint.r2_prefix:
            raise Conflict(f"Checkpoint {checkpoint.id} has no R2 prefix")

        ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
        await self.host.objects.download_dir(checkpoint.r2_prefix, ckpt_dir)
        logger.info("Downloaded snapshot files for checkpoint %s", checkpoint.id)

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise NotFound(f"Computer {computer_id} not found")
        if computer.status == ComputerStatus.DESTROYED:
            logger.warning("Computer %s already destroyed, skipping", computer_id)
            return

        # Remove Caddy route first (so traffic stops immediately)
        await self.host.proxy.remove_route(computer_id)

        # Kill Firecracker (the hypervisor waits for the process to exit)
        if computer.firecracker_pid is not None:
            await self.host.hypervisor.kill(computer.firecracker_pid)

        # Remove dm-thin volume
        volume_name = f"mshkn-{computer_id}"
        await self.host.blocks.remove(volume_id=computer.thin_volume_id, name=volume_name)

        # Remove tap device and recycle slot
        slot = int(computer.tap_device.replace("tap", ""))
        await self.host.hypervisor.teardown_slot(slot)
        async with self._alloc_lock:
            self._release_slot(slot)

        # Clean up SSH pool connection
        if computer.vm_ip:
            await self.host.guest.evict(computer.vm_ip)

        # Update DB
        await update_computer_status(self.db, computer_id, ComputerStatus.DESTROYED)
        logger.info("Destroyed computer %s", computer_id)

    # ── Stale VM Reaper ───────────────────────────────────────────────────

    async def reap_dead_vms(self) -> int:
        """Find VMs whose Firecracker process has died and clean them up.

        Returns the number of VMs reaped.
        """
        computers = await list_all_computers(self.db)
        running = [c for c in computers if c.status == ComputerStatus.RUNNING]
        reaped = 0

        for computer in running:
            if computer.firecracker_pid is None:
                continue
            if self.host.hypervisor.is_alive(computer.firecracker_pid):
                continue

            logger.warning(
                "Reaping dead VM %s (PID %d no longer running)",
                computer.id,
                computer.firecracker_pid,
            )
            try:
                await self._cleanup_dead_vm(computer)
                reaped += 1
            except Exception:
                logger.exception("Failed to reap VM %s", computer.id)

        return reaped

    async def _cleanup_dead_vm(self, computer: Computer) -> None:
        """Clean up resources for a VM whose process is already dead."""
        # Remove Caddy route (never raises)
        await self.host.proxy.remove_route(computer.id)

        # Remove dm-thin volume (process is already dead, no need to wait)
        volume_name = f"mshkn-{computer.id}"
        await self.host.blocks.remove(volume_id=computer.thin_volume_id, name=volume_name)

        # Remove tap device and recycle slot
        slot = int(computer.tap_device.replace("tap", ""))
        try:
            await self.host.hypervisor.teardown_slot(slot)
        except Exception:
            logger.debug("TAP removal failed for %s (may already be gone)", computer.id)
        async with self._alloc_lock:
            self._release_slot(slot)

        # Drop the pooled SSH connection: the slot is back in circulation, and the
        # next VM to land on it would otherwise inherit a connection to a dead VM.
        if computer.vm_ip:
            try:
                await self.host.guest.evict(computer.vm_ip)
            except Exception:
                logger.debug("SSH eviction failed for %s", computer.id)

        # Mark destroyed in DB
        await update_computer_status(self.db, computer.id, ComputerStatus.DESTROYED)
        logger.info("Reaped dead VM %s", computer.id)

    async def reap_idle_vms(self) -> int:
        """Find VMs that have been idle beyond the timeout and auto-checkpoint + destroy.

        Returns the number of VMs reaped.
        """
        if self.config.idle_timeout_seconds <= 0:
            return 0

        computers = await list_all_computers(self.db)
        running = [c for c in computers if c.status == ComputerStatus.RUNNING]
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
                    computer.id,
                    idle_seconds,
                    self.config.idle_timeout_seconds,
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
        """Auto-checkpoint a VM and then destroy it.

        Also drains the deferred queue for the computer's label so that
        queued forks are not permanently stuck.
        """
        import uuid as _uuid

        from mshkn.db import (
            claim_deferred_by_label,
            get_checkpoint,
            get_latest_checkpoint_for_computer,
            insert_checkpoint,
        )
        from mshkn.models import Checkpoint

        # Resolve the original label from the source checkpoint so we can
        # preserve it on the auto-checkpoint and drain the deferred queue.
        original_label: str | None = None
        if computer.source_checkpoint_id:
            source_ckpt = await get_checkpoint(self.db, computer.source_checkpoint_id)
            if source_ckpt is not None:
                original_label = source_ckpt.label

        checkpoint_id = f"ckpt-{_uuid.uuid4().hex[:12]}"
        snapshot_dir = self.config.checkpoint_local_dir / checkpoint_id

        try:
            # Flush guest filesystem (total timeout covers connect + exec)
            await asyncio.wait_for(
                self.host.guest.exec(computer.vm_ip, "sync", timeout=10.0),
                timeout=15.0,
            )

            # Pause/snapshot/resume
            await self.host.hypervisor.snapshot(computer.socket_path, snapshot_dir)

            # Evict SSH pool connection — pause/resume disrupts TCP session
            if computer.vm_ip:
                await self.host.guest.evict(computer.vm_ip)

            # Freeze disk
            ckpt_volume_id = await self.snapshot_disk_for_checkpoint(
                computer,
                checkpoint_id,
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
                r2_prefix=r2_prefix,
                disk_delta_size_bytes=0,
                memory_size_bytes=0,
                label=original_label or "auto-idle-timeout",
                pinned=False,
                created_at=now,
            )
            await insert_checkpoint(self.db, ckpt)
            checkpoints_total.labels(trigger="idle").inc()

            # Upload to R2 in background (best-effort, don't block reaper)
            self.tasks.spawn(
                self._upload_checkpoint_bg(snapshot_dir, r2_prefix, checkpoint_id),
                name=f"upload:{checkpoint_id}",
                key=f"upload:{checkpoint_id}",
            )

            logger.info("Auto-checkpoint %s created for idle VM %s", checkpoint_id, computer.id)
        except Exception:
            logger.exception("Auto-checkpoint failed for VM %s, destroying anyway", computer.id)

        # Destroy the VM
        await self.destroy(computer.id)
        logger.info("Destroyed idle VM %s", computer.id)

        # Drain deferred queue for the original label (must happen AFTER
        # destroy so the new fork doesn't conflict with this computer).
        effective_label = original_label or "auto-idle-timeout"
        deferred = await claim_deferred_by_label(self.db, effective_label)
        if deferred:
            from mshkn.api.computers import _process_deferred
            from mshkn.db import get_account_by_id

            account = await get_account_by_id(self.db, computer.account_id)
            if account is not None:
                self.tasks.spawn(
                    _process_deferred(
                        label=effective_label,
                        deferred_items=deferred,
                        db=self.db,
                        config=self.config,
                        vm_mgr=self,
                        account=account,
                        host=self.host,
                        tasks=self.tasks,
                    ),
                    name=f"deferred:{effective_label}",
                )
                logger.info(
                    "Draining %d deferred item(s) for label '%s' after idle reap",
                    len(deferred),
                    effective_label,
                )

    async def _upload_checkpoint_bg(
        self,
        snapshot_dir: Path,
        r2_prefix: str,
        checkpoint_id: str,
    ) -> None:
        """Background R2 upload for auto-checkpoints."""
        try:
            await self.host.objects.upload_dir(snapshot_dir, r2_prefix)
        except Exception:
            logger.warning(
                "R2 upload failed for auto-checkpoint %s",
                checkpoint_id,
            )

    async def prune_checkpoints(self) -> int:
        """Delete checkpoints that exceed the per-account retention count.

        Pinned checkpoints are never deleted. Returns total pruned count.
        """
        from mshkn.db import (
            delete_checkpoint,
            list_account_ids_with_checkpoints,
            list_prunable_checkpoints,
        )

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
                    ckpt.id,
                    account_id,
                    ckpt.created_at,
                )
                try:
                    # Remove dm-thin volume
                    if ckpt.thin_volume_id is not None:
                        await self.host.blocks.remove(
                            volume_id=ckpt.thin_volume_id,
                            name=f"mshkn-ckpt-{ckpt.id}",
                        )

                    # Remove local snapshot files
                    local_dir = self.config.checkpoint_local_dir / ckpt.id
                    if local_dir.exists():
                        shutil.rmtree(local_dir)

                    # Remove from R2
                    try:
                        await self.host.objects.delete_prefix(ckpt.r2_prefix)
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
                host_ram_used_ratio.set(used_pct / 100.0)
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
            interval,
            idle_timeout,
            retention,
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
                        dead,
                        idle,
                        pruned,
                        len(host_alerts),
                    )
            except Exception:
                logger.exception("Reaper cycle failed")
