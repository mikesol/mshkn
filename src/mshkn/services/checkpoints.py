"""Checkpoints: the one create implementation, delete/prune, merge, and the
locked admission to a labelled chain: fork by label and exclusive fork (spec §6.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from mshkn.errors import BadRequest, Conflict, HostError, NotFound
from mshkn.models import Checkpoint, CheckpointTrigger, Computer, checkpoint_volume_name
from mshkn.observability.metrics import checkpoints_total, timed
from mshkn.services.merge import (
    MergeResult,
    all_relative_entries,
    copy_entry,
    entry_path,
    three_way_merge,
    unlink_stale_ancestors,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.models import Account, ExclusiveMode, ExecSpec
    from mshkn.runtime import BackgroundTasks
    from mshkn.services.allocator import SlotAllocator
    from mshkn.services.computers import ComputerService

logger = logging.getLogger(__name__)

_SYNC_TIMEOUT_SECONDS = 15.0
# How long the tmpfs copy of a snapshot outlives its upload. A fork resolves
# its files before it loads them, so a copy must not vanish under a fork that
# chose it a moment before the durable copy appeared.
_STAGING_LINGER_SECONDS = 30.0
# Room the staging filesystem must have before a snapshot is written there:
# the memory file is the guest's RAM (256 MiB by default, more with custom
# resources) plus vmstate, and a snapshot that runs out of room costs a
# failed attempt and a second pause.
_STAGING_MIN_FREE_BYTES = 2 * 1024**3


def _staging_free_bytes(path: Path) -> int:
    """Free bytes on the filesystem holding path (its nearest existing parent)."""
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


@dataclass(frozen=True)
class MergeOutcome:
    checkpoint: Checkpoint
    conflicts: list[str]
    auto_merged: int
    unchanged: int


@dataclass(frozen=True)
class Deferred:
    deferred_id: str


@dataclass
class _LabelLock:
    """One label's admission lock and the number of holders and waiters on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


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
        # Admission to a labelled chain is serialised per (account, label): head
        # resolution, the active-computer check and the computer insert happen
        # under one lock, so two forks cannot both pass the check before either
        # has a row, and a fork by label cannot read a head that a concurrent
        # fork is about to replace. Process-local is correct: the server is one
        # asyncio process (the socket registry in §5 is process-local for the
        # same reason). The exec runs outside the lock. An entry exists only
        # while someone holds or waits for it, so labels do not accumulate.
        self._label_locks: dict[tuple[str, str], _LabelLock] = {}
        # Uploads go one at a time: several 256 MiB rclone copies at once against
        # the disk the next checkpoint writes to is what made T1.2 decay (#145).
        self._upload_slot = asyncio.Semaphore(1)

    @staticmethod
    def upload_task_key(checkpoint_id: str) -> str:
        return f"upload:{checkpoint_id}"

    @staticmethod
    def staging_clear_task_key(checkpoint_id: str) -> str:
        return f"stage-clear:{checkpoint_id}"

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

    async def source_label(self, account_id: str, computer: Computer) -> str | None:
        """The label of the checkpoint a computer was forked from, if it still exists.

        A tolerant lookup, not get_owned(): prune() deletes checkpoints without
        checking for live forks, so a destroy or an idle reap must not turn into
        a 404 just because the source row has since gone.
        """
        if not computer.source_checkpoint_id:
            return None
        source = await get_checkpoint(self.db, computer.source_checkpoint_id)
        if source is None or source.account_id != account_id or not source.label:
            return None
        return source.label

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
        # Written on tmpfs: Firecracker fsyncs the memory file, and on the
        # durable disk that alone was 300 ms and more per checkpoint (#144).
        staging_dir = self.config.checkpoint_staging_dir / checkpoint_id
        durable_dir = self.config.checkpoint_local_dir / checkpoint_id
        staged = _staging_free_bytes(self.config.checkpoint_staging_dir) >= _STAGING_MIN_FREE_BYTES
        if not staged:
            logger.warning(
                "Staging filesystem under %s has less than %d MiB free; writing %s to %s",
                self.config.checkpoint_staging_dir,
                _STAGING_MIN_FREE_BYTES // 1024**2,
                checkpoint_id,
                durable_dir,
            )
        async with timed("checkpoint"):
            # Flush the guest's page cache to the block device: dm-thin snapshots
            # see only what reached the disk.
            await asyncio.wait_for(
                self.host.guest.exec(computer.vm_ip, "sync", timeout=10.0),
                timeout=_SYNC_TIMEOUT_SECONDS,
            )
            # The disk snap must follow the memory snapshot, not overlap it: the
            # guest's `sync` does not reach the host disk (Firecracker's drive
            # cache is Unsafe, so guest flushes are ignored), and it is
            # Firecracker's own flush inside create_snapshot that lands the
            # guest's writes on the thin volume. A snap taken alongside the dump
            # raced that flush and captured empty files (found by the live run
            # of #147). The pooled SSH session is kept across the pause: a pause
            # of a few hundred milliseconds does not break a TCP connection, and
            # the unconditional evict cost the next exec a full handshake (#150).
            if staged:
                try:
                    await self.host.hypervisor.snapshot(computer.socket_path, staging_dir)
                except HostError:
                    # The room check above is the identified case; this is the
                    # one retry for what it did not foresee (tmpfs filling in the
                    # meantime, say). `snapshot` resumes the guest whatever
                    # happened, so a second attempt is safe, and the first
                    # failure is logged with its traceback, not hidden.
                    logger.warning(
                        "Snapshot of %s onto %s failed; writing it to %s instead",
                        computer.id,
                        staging_dir,
                        durable_dir,
                        exc_info=True,
                    )
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    staged = False
            if not staged:
                await self.host.hypervisor.snapshot(computer.socket_path, durable_dir)
            volume_name = checkpoint_volume_name(checkpoint_id)
            volume_id = await self._snap_disk(computer.thin_volume_id, volume_name)
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
            self._persist_and_upload(checkpoint_id, ckpt.r2_prefix, staged=staged),
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

    async def _snap_disk(self, source_volume_id: int, volume_name: str) -> int:
        """A new thin volume snapped from the computer's, mapped under volume_name."""
        volume_id = await self.allocator.acquire_volume_id()
        await self.host.blocks.snap(source_volume_id=source_volume_id, new_volume_id=volume_id)
        await self.host.blocks.activate(volume_id=volume_id, name=volume_name)
        return volume_id

    async def _persist_and_upload(
        self, checkpoint_id: str, r2_prefix: str, *, staged: bool
    ) -> None:
        """Copy the snapshot from tmpfs to the durable directory, release the
        tmpfs copy after a linger, and upload.

        The durable copy is built under `<id>.tmp` and renamed into place, so
        it is complete whenever it exists, and the linger starts the moment it
        does: uploads run one at a time and take tens of seconds each, so a
        tmpfs copy held until its upload ended would pile up with the others
        until the tmpfs was full. A copy that fails leaves the tmpfs copy as
        the only one: it is uploaded from there and kept. `staged` is False
        when the snapshot was written to the durable directory in the first
        place, in which case there is nothing to persist or release.
        """
        staging_dir = self.config.checkpoint_staging_dir / checkpoint_id
        durable_dir = self.config.checkpoint_local_dir / checkpoint_id
        source = durable_dir
        if staged:
            try:
                await asyncio.to_thread(_persist_snapshot, staging_dir, durable_dir)
            except Exception:
                logger.warning(
                    "Could not persist checkpoint %s to %s; keeping the staging copy",
                    checkpoint_id,
                    durable_dir,
                    exc_info=True,
                )
                source = staging_dir
            else:
                self.tasks.spawn(
                    self._clear_staging(staging_dir),
                    name=self.staging_clear_task_key(checkpoint_id),
                    key=self.staging_clear_task_key(checkpoint_id),
                )
        async with self._upload_slot:
            try:
                await self.host.objects.upload_dir(source, r2_prefix)
            except Exception:
                logger.warning("R2 upload failed for checkpoint %s", checkpoint_id, exc_info=True)

    async def recover_staging(self) -> int:
        """Finish what a previous process's persist tasks left in the staging
        directory: persist every complete copy that has a checkpoint row and no
        durable twin, then empty the directory. Called once at start-up. Returns how many were
        persisted; an incomplete copy (a snapshot the process died inside)
        has nothing worth keeping and is removed with the rest.
        """
        staging_root = self.config.checkpoint_staging_dir
        if not staging_root.is_dir():
            return 0
        persisted = 0
        for entry in sorted(staging_root.iterdir()):
            durable_dir = self.config.checkpoint_local_dir / entry.name
            complete = (entry / "vmstate").exists() and (entry / "memory").exists()
            # Only a checkpoint with a row is worth persisting: a snapshot the
            # process died between writing and inserting the row for belongs
            # to nothing, and nothing could ever delete its durable copy.
            has_row = complete and await get_checkpoint(self.db, entry.name) is not None
            if has_row and not durable_dir.exists():
                try:
                    await asyncio.to_thread(_persist_snapshot, entry, durable_dir)
                    persisted += 1
                except Exception:
                    logger.warning(
                        "Could not persist staged checkpoint %s", entry.name, exc_info=True
                    )
                    continue
            shutil.rmtree(entry, ignore_errors=True)
        return persisted

    @staticmethod
    async def _clear_staging(staging_dir: Path) -> None:
        await asyncio.sleep(_STAGING_LINGER_SECONDS)
        shutil.rmtree(staging_dir, ignore_errors=True)

    # -- delete / prune ------------------------------------------------------

    async def delete(self, checkpoint: Checkpoint) -> None:
        await self.tasks.cancel(self.upload_task_key(checkpoint.id))
        await self.tasks.cancel(self.staging_clear_task_key(checkpoint.id))
        if checkpoint.thin_volume_id is not None:
            await self.host.blocks.remove(
                volume_id=checkpoint.thin_volume_id, name=checkpoint.volume_name
            )
        for base in (self.config.checkpoint_local_dir, self.config.checkpoint_staging_dir):
            shutil.rmtree(base / checkpoint.id, ignore_errors=True)
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
        merged_volume_name = checkpoint_volume_name(checkpoint_id)
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

    @asynccontextmanager
    async def _label_lock(self, account_id: str, label: str) -> AsyncIterator[None]:
        key = (account_id, label)
        entry = self._label_locks.get(key)
        if entry is None:
            entry = self._label_locks[key] = _LabelLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._label_locks.get(key) is entry:
                del self._label_locks[key]

    async def fork_by_label(
        self,
        account: Account,
        label: str,
        spec: ExecSpec,
        *,
        exclusive: ExclusiveMode | None,
        recipe_id: str | None,
        api_key_id: str | None = None,
    ) -> tuple[Checkpoint, Computer | Deferred]:
        """Advance the chain `label` as one operation: resolve its head, admit
        the fork, and insert the computer row, all under the label's lock.

        Returns the head that was forked (or that the deferral was queued
        against) with the outcome. NotFound when no checkpoint carries the label.
        """
        async with self._label_lock(account.id, label):
            head = await self.latest_for_label(account, label)
            if head is None:
                raise NotFound(f"No checkpoint with label '{label}'")
            outcome = await self._admit(
                account,
                head,
                spec,
                recipe_id=recipe_id,
                exclusive=exclusive,
                api_key_id=api_key_id,
            )
        return head, outcome

    async def fork_or_defer(
        self,
        account: Account,
        checkpoint: Checkpoint,
        spec: ExecSpec,
        *,
        recipe_id: str | None,
        exclusive: ExclusiveMode | None,
        api_key_id: str | None = None,
    ) -> Computer | Deferred:
        """Fork a checkpoint by id. A labelled checkpoint takes its label's lock,
        so a fork by id cannot slip past a concurrent fork by label."""
        if not checkpoint.label:
            return await self._admit(
                account,
                checkpoint,
                spec,
                recipe_id=recipe_id,
                exclusive=exclusive,
                api_key_id=api_key_id,
            )
        async with self._label_lock(account.id, checkpoint.label):
            return await self._admit(
                account,
                checkpoint,
                spec,
                recipe_id=recipe_id,
                exclusive=exclusive,
                api_key_id=api_key_id,
            )

    async def _admit(
        self,
        account: Account,
        checkpoint: Checkpoint,
        spec: ExecSpec,
        *,
        recipe_id: str | None,
        exclusive: ExclusiveMode | None,
        api_key_id: str | None,
    ) -> Computer | Deferred:
        """The active-computer check and the fork; the caller holds the label's lock.
        `api_key_id` is the scoped key forking, recorded on the new computer (#88)."""
        if exclusive is not None and checkpoint.label:
            active = await get_active_computer_for_label(self.db, account.id, checkpoint.label)
            if active is not None:
                if exclusive == "error_on_conflict":
                    raise Conflict("Checkpoint chain has active computer")
                deferred_id = f"def-{uuid.uuid4().hex[:12]}"
                payload = {
                    # Informational: the drain forks from the newest checkpoint
                    # carrying the label, not from this one.
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
        return await self.computers.fork(
            account, checkpoint, recipe_id=recipe_id, api_key_id=api_key_id
        )


def _persist_snapshot(staging_dir: Path, durable_dir: Path) -> None:
    """Copy a snapshot directory into place atomically. Blocking."""
    tmp = durable_dir.with_name(f"{durable_dir.name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(staging_dir, tmp)
    tmp.rename(durable_dir)


def _merge_into(parent: Path, fork_a: Path, fork_b: Path, output: Path) -> MergeResult:
    """Three-way merge into a scratch dir, then apply it onto the output mount. Blocking."""
    with tempfile.TemporaryDirectory(prefix="mshkn-merge-") as merge_dir:
        merge_output = Path(merge_dir) / "merge_result"
        result = three_way_merge(parent=parent, fork_a=fork_a, fork_b=fork_b, output=merge_output)
        # Symlinks are entries, never paths to follow: an absolute link on a
        # mounted volume resolves to the host's tree, so copying through one
        # would read and overwrite the host's files.
        merged = all_relative_entries(merge_output)
        unlink_stale_ancestors(output, merged)
        # Sorted, so a link that replaced a directory lands before anything
        # that path used to hold; `entry_path` then reports the children of
        # that directory as unreachable rather than writing through the link.
        for rel in sorted(merged):
            dest = entry_path(output, rel)
            if dest is not None:
                copy_entry(merge_output / rel, dest)
        for rel in sorted(all_relative_entries(parent) - merged):
            # A real directory here is the merge result's, put there by a fork
            # that replaced the parent's file or link with one; only the
            # parent's own kind of entry is deleted.
            target = entry_path(output, rel)
            if target is not None and (target.is_symlink() or target.is_file()):
                target.unlink()
    return result
