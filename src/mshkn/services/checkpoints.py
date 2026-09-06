"""Checkpoints: the one create implementation, delete/prune, merge, exclusive fork (spec §6.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mshkn.db import (
    delete_checkpoint,
    get_active_computer_for_label,
    get_checkpoint,
    get_latest_checkpoint_for_computer,
    insert_checkpoint,
    insert_deferred,
    list_account_ids_with_checkpoints,
    list_checkpoints_by_account,
    list_prunable_checkpoints,
)
from mshkn.errors import BadRequest, Conflict, NotFound
from mshkn.models import Checkpoint, CheckpointTrigger, Computer
from mshkn.observability.metrics import checkpoints_total, timed
from mshkn.services.merge import MergeResult, three_way_merge

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.models import Account, ExclusiveMode, ExecSpec
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.allocator import SlotAllocator
    from mshkn.services.computers import ComputerService

logger = logging.getLogger(__name__)

_SYNC_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class MergeOutcome:
    checkpoint: Checkpoint
    conflicts: list[str]
    auto_merged: int
    unchanged: int


@dataclass(frozen=True)
class Deferred:
    deferred_id: str


class CheckpointService:
    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        host: Host,
        allocator: SlotAllocator,
        computers: ComputerService,
        tasks: BackgroundTasks,
    ) -> None:
        self.config = config
        self.db = db
        self.host = host
        self.allocator = allocator
        self.computers = computers
        self.tasks = tasks

    @staticmethod
    def upload_task_key(checkpoint_id: str) -> str:
        return f"upload:{checkpoint_id}"

    # -- lookups -------------------------------------------------------------

    async def get_owned(self, account: Account, checkpoint_id: str) -> Checkpoint:
        ckpt = await get_checkpoint(self.db, checkpoint_id)
        if ckpt is None or ckpt.account_id != account.id:
            raise NotFound("Checkpoint not found")
        return ckpt

    async def list(self, account: Account, *, label: str | None = None) -> list[Checkpoint]:
        return await list_checkpoints_by_account(self.db, account.id, label=label)

    async def latest_for_label(self, account: Account, label: str) -> Checkpoint | None:
        ckpts = await list_checkpoints_by_account(self.db, account.id, label=label)
        return ckpts[0] if ckpts else None

    # -- create --------------------------------------------------------------

    async def create(
        self,
        computer: Computer,
        *,
        label: str | None,
        pin: bool = False,
        trigger: CheckpointTrigger,
    ) -> Checkpoint:
        checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
        snapshot_dir = self.config.checkpoint_local_dir / checkpoint_id
        async with timed("checkpoint"):
            # Flush the guest's page cache to the block device: dm-thin snapshots
            # see only what reached the disk.
            await asyncio.wait_for(
                self.host.guest.exec(computer.vm_ip, "sync", timeout=10.0),
                timeout=_SYNC_TIMEOUT_SECONDS,
            )
            await self.host.hypervisor.snapshot(computer.socket_path, snapshot_dir)
            # pause/resume breaks the pooled TCP session
            await self.host.guest.evict(computer.vm_ip)
            volume_id = await self.allocator.acquire_volume_id()
            volume_name = f"mshkn-ckpt-{checkpoint_id}"
            await self.host.blocks.snap(
                source_volume_id=computer.thin_volume_id, new_volume_id=volume_id
            )
            await self.host.blocks.activate(volume_id=volume_id, name=volume_name)
            latest = await get_latest_checkpoint_for_computer(self.db, computer.id)
            if latest is not None:
                parent_id: str | None = latest.id
            else:
                parent_id = computer.source_checkpoint_id
            ckpt = Checkpoint(
                id=checkpoint_id,
                account_id=computer.account_id,
                parent_id=parent_id,
                computer_id=computer.id,
                thin_volume_id=volume_id,
                r2_prefix=f"{computer.account_id}/{checkpoint_id}",
                disk_delta_size_bytes=None,
                memory_size_bytes=None,
                label=label,
                pinned=pin,
                created_at=datetime.now(UTC).isoformat(),
                recipe_id=computer.recipe_id,
            )
            await insert_checkpoint(self.db, ckpt)
        checkpoints_total.labels(trigger=trigger.value).inc()
        self.tasks.spawn(
            self._upload(snapshot_dir, ckpt.r2_prefix, checkpoint_id),
            name=self.upload_task_key(checkpoint_id),
            key=self.upload_task_key(checkpoint_id),
        )
        logger.info(
            "Checkpoint %s created for %s",
            checkpoint_id,
            computer.id,
            extra={
                "op": "checkpoint",
                "checkpoint_id": checkpoint_id,
                "computer_id": computer.id,
                "trigger": trigger.value,
            },
        )
        return ckpt

    async def _upload(self, snapshot_dir: Path, r2_prefix: str, checkpoint_id: str) -> None:
        try:
            await self.host.objects.upload_dir(snapshot_dir, r2_prefix)
        except Exception:
            logger.warning("R2 upload failed for checkpoint %s", checkpoint_id, exc_info=True)

    # -- delete / prune ------------------------------------------------------

    async def delete(self, checkpoint: Checkpoint) -> None:
        await self.tasks.cancel(self.upload_task_key(checkpoint.id))
        if checkpoint.thin_volume_id is not None:
            await self.host.blocks.remove(
                volume_id=checkpoint.thin_volume_id, name=checkpoint.volume_name
            )
        local_dir = self.config.checkpoint_local_dir / checkpoint.id
        shutil.rmtree(local_dir, ignore_errors=True)
        await self.host.objects.delete_prefix(checkpoint.r2_prefix)
        await delete_checkpoint(self.db, checkpoint.id)

    async def prune(self) -> int:
        keep = self.config.checkpoint_retention_count
        if keep <= 0:
            return 0
        pruned = 0
        for account_id in await list_account_ids_with_checkpoints(self.db):
            for ckpt in await list_prunable_checkpoints(self.db, account_id, keep):
                try:
                    await self.delete(ckpt)
                    pruned += 1
                    logger.info("Pruned checkpoint %s (account=%s)", ckpt.id, account_id)
                except Exception:
                    logger.exception("Failed to prune checkpoint %s", ckpt.id)
        return pruned

    # -- merge ---------------------------------------------------------------

    async def merge(self, account: Account, parent_id: str, a_id: str, b_id: str) -> MergeOutcome:
        parent = await get_checkpoint(self.db, parent_id)
        if parent is None or parent.account_id != account.id:
            raise NotFound("Parent checkpoint not found")
        if a_id == b_id:
            raise BadRequest("Cannot merge a checkpoint with itself")
        a = await get_checkpoint(self.db, a_id)
        b = await get_checkpoint(self.db, b_id)
        if a is None or a.account_id != account.id:
            raise NotFound("Checkpoint A not found")
        if b is None or b.account_id != account.id:
            raise NotFound("Checkpoint B not found")
        if a.parent_id != parent_id or b.parent_id != parent_id:
            raise BadRequest("Both checkpoints must be children of the specified parent")
        for name, ckpt in (("Parent", parent), ("A", a), ("B", b)):
            if ckpt.thin_volume_id is None:
                raise BadRequest(f"{name} checkpoint has no disk snapshot")
        assert parent.thin_volume_id is not None

        checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
        merged_volume_id = await self.allocator.acquire_volume_id()
        merged_volume_name = f"mshkn-ckpt-{checkpoint_id}"
        async with timed("merge"):
            await self.host.blocks.snap(
                source_volume_id=parent.thin_volume_id, new_volume_id=merged_volume_id
            )
            await self.host.blocks.activate(volume_id=merged_volume_id, name=merged_volume_name)
            async with (
                self.host.blocks.mounted(parent.volume_name, readonly=True) as mount_parent,
                self.host.blocks.mounted(a.volume_name, readonly=True) as mount_a,
                self.host.blocks.mounted(b.volume_name, readonly=True) as mount_b,
                self.host.blocks.mounted(merged_volume_name) as mount_output,
            ):
                result = await asyncio.to_thread(
                    _merge_into, mount_parent, mount_a, mount_b, mount_output
                )
        ckpt = Checkpoint(
            id=checkpoint_id,
            account_id=account.id,
            parent_id=parent_id,
            computer_id=None,
            thin_volume_id=merged_volume_id,
            r2_prefix=f"{account.id}/{checkpoint_id}",
            disk_delta_size_bytes=None,
            memory_size_bytes=None,
            label="merge",
            pinned=False,
            created_at=datetime.now(UTC).isoformat(),
            recipe_id=parent.recipe_id,
        )
        await insert_checkpoint(self.db, ckpt)
        logger.info(
            "Merged checkpoint %s: auto_merged=%d, unchanged=%d, conflicts=%d",
            checkpoint_id,
            result.auto_merged,
            result.unchanged,
            len(result.conflicts),
        )
        return MergeOutcome(
            checkpoint=ckpt,
            conflicts=[c.path for c in result.conflicts],
            auto_merged=result.auto_merged,
            unchanged=result.unchanged,
        )

    # -- exclusive fork ------------------------------------------------------

    async def fork_or_defer(
        self,
        account: Account,
        checkpoint: Checkpoint,
        spec: ExecSpec,
        *,
        recipe_id: str | None,
        exclusive: ExclusiveMode | None,
    ) -> Computer | Deferred:
        if exclusive is not None and checkpoint.label:
            active = await get_active_computer_for_label(self.db, account.id, checkpoint.label)
            if active is not None:
                if exclusive == "error_on_conflict":
                    raise Conflict("Checkpoint chain has active computer")
                deferred_id = f"def-{uuid.uuid4().hex[:12]}"
                payload = {
                    "checkpoint_id": checkpoint.id,
                    "recipe_id": recipe_id,
                    "exec": spec.command,
                    "self_destruct": spec.self_destruct,
                    "callback_url": spec.callback_url,
                    "meta_exec": spec.meta_exec,
                }
                await insert_deferred(
                    self.db,
                    deferred_id,
                    checkpoint.label,
                    account.id,
                    json.dumps(payload),
                    datetime.now(UTC).isoformat(),
                )
                return Deferred(deferred_id)
        return await self.computers.fork(account, checkpoint, recipe_id=recipe_id)


def _merge_into(parent: Path, fork_a: Path, fork_b: Path, output: Path) -> MergeResult:
    """Three-way merge into a scratch dir, then apply it onto the output mount. Blocking."""
    with tempfile.TemporaryDirectory(prefix="mshkn-merge-") as merge_dir:
        merge_output = Path(merge_dir) / "merge_result"
        result = three_way_merge(parent=parent, fork_a=fork_a, fork_b=fork_b, output=merge_output)
        for src in merge_output.rglob("*"):
            if src.is_file():
                dest = output / src.relative_to(merge_output)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
        for src in parent.rglob("*"):
            if src.is_file():
                rel = src.relative_to(parent)
                if not (merge_output / rel).exists():
                    target = output / rel
                    if target.exists():
                        target.unlink()
    return result
