"""Computers: create, fork, destroy, and guest operations (spec §6.2)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.db import (
    count_active_computers,
    count_active_computers_by_account,
    get_computer,
    insert_computer,
    update_computer_status,
    update_last_exec_at,
)
from mshkn.errors import BadRequest, Conflict, HostError, LimitExceeded, MshknError, NotFound
from mshkn.host import SnapshotFiles
from mshkn.models import Computer, ComputerStatus
from mshkn.observability.metrics import computers_active, computers_created_total, timed
from mshkn.resources import DEFAULT_RESOURCES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import ExecResult, Host, OutputLine, RunningVM, VmMetrics
    from mshkn.models import Account, Checkpoint, Recipe
    from mshkn.resources import Resources
    from mshkn.services.allocator import SlotAllocator
    from mshkn.services.recipes import RecipeService

logger = logging.getLogger(__name__)

STATUS_METRICS_TIMEOUT_SECONDS = 15.0


async def _best_effort(what: str, action: Awaitable[object], computer_id: str) -> None:
    """Await a cleanup step, swallowing and logging anything it raises."""
    try:
        await action
    except Exception:
        logger.debug("%s during abandon failed for %s", what, computer_id, exc_info=True)


class ComputerService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        allocator: SlotAllocator,
        recipes: RecipeService,
    ) -> None:
        self.config = config
        self.db = db
        self.host = host
        self.allocator = allocator
        self.recipes = recipes

    # -- lookups -------------------------------------------------------------

    async def get_owned(self, account: Account, computer_id: str) -> Computer:
        computer = await get_computer(self.db, computer_id)
        if (
            computer is None
            or computer.account_id != account.id
            or computer.status == ComputerStatus.DESTROYED
        ):
            raise NotFound("Computer not found")
        return computer

    async def get_running(self, account: Account, computer_id: str) -> Computer:
        computer = await get_computer(self.db, computer_id)
        if computer is None or computer.account_id != account.id:
            raise NotFound("Computer not found")
        if computer.status != ComputerStatus.RUNNING:
            raise BadRequest(f"Computer is {computer.status}")
        return computer

    async def active_count(self, account_id: str) -> int:
        return await count_active_computers_by_account(self.db, account_id)

    async def active_count_total(self) -> int:
        return await count_active_computers(self.db)

    async def refresh_active_gauge(self) -> int:
        total = await self.active_count_total()
        computers_active.set(total)
        return total

    # -- create / fork -------------------------------------------------------

    async def create(
        self, account: Account, *, recipe_id: str | None, resources: Resources
    ) -> Computer:
        # timed() wraps the preconditions too, so a rejected create still counts
        # as an error of kind="domain".
        async with timed("create"):
            if await self.active_count(account.id) >= account.vm_limit:
                raise LimitExceeded("VM limit reached")
            recipe: Recipe | None = None
            if recipe_id is not None:
                recipe = await self.recipes.resolve(recipe_id)
            source_volume_id = recipe.base_volume_id if recipe is not None else 0
            if source_volume_id is None:  # resolve() rejects this; belt and braces
                raise Conflict(f"Recipe {recipe_id} has no base volume")
            computer = await self._bring_up(
                account,
                source_volume_id=source_volume_id,
                recipe_id=recipe_id,
                source_checkpoint=None,
                resources=resources,
                files_for=lambda: self._template_for(recipe, resources),
            )
        computers_created_total.labels(source="create").inc()
        logger.info(
            "Created computer %s (slot=%d, ip=%s)", computer.id, computer.slot, computer.vm_ip
        )
        return computer

    async def fork(
        self, account: Account, checkpoint: Checkpoint, *, recipe_id: str | None
    ) -> Computer:
        async with timed("fork"):
            if checkpoint.thin_volume_id is None:
                raise Conflict(f"Checkpoint {checkpoint.id} has no disk snapshot")
            effective_recipe_id = recipe_id if recipe_id is not None else checkpoint.recipe_id
            computer = await self._bring_up(
                account,
                source_volume_id=checkpoint.thin_volume_id,
                recipe_id=effective_recipe_id,
                source_checkpoint=checkpoint,
                resources=DEFAULT_RESOURCES,
                files_for=lambda: self._snapshot_files_for(checkpoint),
            )
        computers_created_total.labels(source="fork").inc()
        logger.info(
            "Forked computer %s from checkpoint %s (slot=%d, ip=%s)",
            computer.id,
            checkpoint.id,
            computer.slot,
            computer.vm_ip,
        )
        return computer

    async def _template_for(
        self, recipe: Recipe | None, resources: Resources
    ) -> SnapshotFiles | None:
        # Templates bake in the default resources; anything else cold-boots.
        if not resources.is_default:
            logger.info(
                "Cold-booting with custom resources: mem=%dMiB, vcpu=%d",
                resources.mem_mib,
                resources.vcpus,
            )
            return None
        return await self.recipes.ensure_template(recipe)

    async def _snapshot_files_for(self, checkpoint: Checkpoint) -> SnapshotFiles | None:
        ckpt_dir = self.config.checkpoint_local_dir / checkpoint.id
        files = SnapshotFiles(vmstate=ckpt_dir / "vmstate", memory=ckpt_dir / "memory")
        if files.vmstate.exists() and files.memory.exists():
            return files
        if not checkpoint.r2_prefix:
            logger.info("Checkpoint %s has no R2 prefix, will cold-boot", checkpoint.id)
            return None
        try:
            await self.host.objects.download_dir(checkpoint.r2_prefix, ckpt_dir)
        except Exception:
            logger.info("No snapshot files for checkpoint %s, will cold-boot", checkpoint.id)
            return None
        if files.vmstate.exists() and files.memory.exists():
            return files
        return None

    async def _bring_up(
        self,
        account: Account,
        *,
        source_volume_id: int,
        recipe_id: str | None,
        source_checkpoint: Checkpoint | None,
        resources: Resources,
        files_for: Callable[[], Awaitable[SnapshotFiles | None]],
    ) -> Computer:
        """Snap the disk, boot or restore, warm SSH, record, route.

        Everything after the snap is guarded: on any failure the VM (if any)
        is killed, the route removed, the volume removed, the tap torn down,
        the slot released, and the error re-raised as HostError.
        """
        computer_id = f"comp-{uuid.uuid4().hex[:12]}"
        volume_name = f"mshkn-{computer_id}"
        slot, volume_id = await self.allocator.acquire()
        try:
            await self.host.blocks.snap(source_volume_id=source_volume_id, new_volume_id=volume_id)
        except BaseException:
            await self.allocator.release_slot(slot)
            raise
        vm: RunningVM | None = None
        routed = False
        try:
            files = await files_for()
            if files is not None:
                vm = await self.host.hypervisor.restore(
                    slot=slot, disk_volume_id=volume_id, disk_name=volume_name, snapshot=files
                )
            else:
                vm = await self.host.hypervisor.boot(
                    slot=slot, disk_volume_id=volume_id, disk_name=volume_name, resources=resources
                )
            await self.host.guest.warm(vm.vm_ip)
            computer = Computer(
                id=computer_id,
                account_id=account.id,
                thin_volume_id=volume_id,
                tap_device=vm.tap_device,
                vm_ip=vm.vm_ip,
                socket_path=vm.socket_path,
                firecracker_pid=vm.pid,
                status=ComputerStatus.RUNNING,
                created_at=datetime.now(UTC).isoformat(),
                last_exec_at=None,
                source_checkpoint_id=source_checkpoint.id if source_checkpoint else None,
                recipe_id=recipe_id,
            )
            await insert_computer(self.db, computer)
            await self.host.proxy.add_route(computer_id, vm.vm_ip)
            routed = True
        except BaseException as exc:
            await self._abandon(computer_id, slot, volume_id, volume_name, vm, routed)
            if isinstance(exc, MshknError | asyncio.CancelledError):
                raise
            raise HostError(
                f"bring-up of {computer_id} failed: {type(exc).__name__}: {exc}"
            ) from exc
        await self.refresh_active_gauge()
        return computer

    async def _abandon(
        self,
        computer_id: str,
        slot: int,
        volume_id: int,
        volume_name: str,
        vm: RunningVM | None,
        routed: bool,
    ) -> None:
        """Best-effort release of everything _bring_up acquired. Never raises.

        Every step is guarded individually and the slot is released in a
        ``finally``, so one failing host call can neither strand the slot nor
        replace the error the caller is about to raise.
        """
        logger.warning("Abandoning computer %s after a failed bring-up", computer_id)
        try:
            if routed:
                await _best_effort(
                    "route removal", self.host.proxy.remove_route(computer_id), computer_id
                )
            if vm is not None:
                await _best_effort("kill", self.host.hypervisor.kill(vm.pid), computer_id)
                await _best_effort("evict", self.host.guest.evict(vm.vm_ip), computer_id)
            await _best_effort(
                "volume removal",
                self.host.blocks.remove(volume_id=volume_id, name=volume_name),
                computer_id,
            )
            await _best_effort("teardown", self.host.hypervisor.teardown_slot(slot), computer_id)
        finally:
            await _best_effort("slot release", self.allocator.release_slot(slot), computer_id)
        await _best_effort("status update", self._mark_destroyed(computer_id), computer_id)

    async def _mark_destroyed(self, computer_id: str) -> None:
        stored = await get_computer(self.db, computer_id)
        if stored is not None:
            await update_computer_status(self.db, computer_id, ComputerStatus.DESTROYED)

    # -- destroy -------------------------------------------------------------

    async def destroy(self, computer_id: str) -> None:
        computer = await get_computer(self.db, computer_id)
        if computer is None:
            raise NotFound(f"Computer {computer_id} not found")
        if computer.status == ComputerStatus.DESTROYED:
            logger.info("Computer %s already destroyed", computer_id)
            return
        async with timed("destroy"):
            await self.host.proxy.remove_route(computer_id)
            if computer.firecracker_pid is not None:
                await self.host.hypervisor.kill(computer.firecracker_pid)
            await self.host.blocks.remove(
                volume_id=computer.thin_volume_id, name=computer.volume_name
            )
            await self.host.hypervisor.teardown_slot(computer.slot)
            await self.allocator.release_slot(computer.slot)
            if computer.vm_ip:
                await self.host.guest.evict(computer.vm_ip)
            await update_computer_status(self.db, computer_id, ComputerStatus.DESTROYED)
        await self.refresh_active_gauge()
        logger.info("Destroyed computer %s", computer_id)

    async def cleanup_dead(self, computer: Computer) -> None:
        """Release a VM whose Firecracker process is already gone. Every step is best-effort."""
        await self.host.proxy.remove_route(computer.id)
        await self.host.blocks.remove(volume_id=computer.thin_volume_id, name=computer.volume_name)
        try:
            await self.host.hypervisor.teardown_slot(computer.slot)
        except Exception:
            logger.debug("TAP removal failed for %s (may already be gone)", computer.id)
        await self.allocator.release_slot(computer.slot)
        if computer.vm_ip:
            try:
                await self.host.guest.evict(computer.vm_ip)
            except Exception:
                logger.debug("SSH eviction failed for %s", computer.id)
        await update_computer_status(self.db, computer.id, ComputerStatus.DESTROYED)
        await self.refresh_active_gauge()
        logger.info("Reaped dead VM %s", computer.id)

    # -- guest operations ----------------------------------------------------

    async def _touch(self, computer: Computer) -> None:
        await update_last_exec_at(self.db, computer.id, datetime.now(UTC).isoformat())

    async def exec(self, computer: Computer, command: str, *, timeout: float = 300.0) -> ExecResult:
        await self._touch(computer)
        async with timed("exec"):
            return await self.host.guest.exec(computer.vm_ip, command, timeout=timeout)

    async def stream(self, computer: Computer, command: str) -> AsyncIterator[OutputLine]:
        await self._touch(computer)
        async for item in self.host.guest.stream(computer.vm_ip, command):
            yield item

    async def exec_bg(self, computer: Computer, command: str) -> int:
        await self._touch(computer)
        return await self.host.guest.exec_bg(computer.vm_ip, command)

    async def exec_logs(self, computer: Computer, pid: int) -> list[str]:
        result = await self.host.guest.exec(
            computer.vm_ip, f"cat /tmp/bg-{pid}.log 2>/dev/null || echo ''", timeout=10.0
        )
        return result.stdout.splitlines()

    async def exec_kill(self, computer: Computer, pid: int) -> ExecResult:
        return await self.host.guest.exec(computer.vm_ip, f"kill {pid}")

    async def upload(self, computer: Computer, remote_path: str, data: bytes) -> None:
        await self.host.guest.upload(computer.vm_ip, remote_path, data)

    async def download(self, computer: Computer, remote_path: str) -> bytes:
        try:
            return await self.host.guest.download(computer.vm_ip, remote_path)
        except FileNotFoundError:
            raise NotFound(f"File not found: {remote_path}") from None

    async def metrics(self, computer: Computer) -> VmMetrics | None:
        try:
            return await asyncio.wait_for(
                self.host.guest.metrics(computer.vm_ip, timeout=10.0),
                timeout=STATUS_METRICS_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Failed to gather metrics for %s: %s", computer.id, type(exc).__name__)
            return None
