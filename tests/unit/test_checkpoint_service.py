from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_checkpoint, insert_account, insert_checkpoint
from mshkn.errors import BadRequest, Conflict, NotFound
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Account, Checkpoint, CheckpointTrigger, Computer, ExecSpec
from mshkn.observability.metrics import checkpoints_total
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService, Deferred
from mshkn.services.computers import ComputerService
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")
OTHER = Account(id="acct-2", api_key="k2", vm_limit=10, created_at="t")
SPEC = ExecSpec(
    command="echo hi", self_destruct=True, callback_url=None, label=None, meta_exec=None
)


def _row(
    ckpt_id: str, *, account_id: str, parent_id: str | None, volume_id: int | None
) -> Checkpoint:
    """A checkpoint row built by hand, for the validation branches create() cannot reach."""
    return Checkpoint(
        id=ckpt_id,
        account_id=account_id,
        parent_id=parent_id,
        computer_id=None,
        thin_volume_id=volume_id,
        r2_prefix=f"{account_id}/{ckpt_id}",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="2026-09-06T00:00:00",
    )


async def _services(
    db: aiosqlite.Connection, tmp_path: Path, *, retention: int = 20
) -> tuple[CheckpointService, ComputerService, FakeHostInstance]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "ckpts",
        checkpoint_retention_count=retention,
    )
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    return checkpoints, computers, host


def _labelled(trigger: str) -> float:
    return float(checkpoints_total.labels(trigger=trigger)._value.get())


async def test_create_runs_the_five_steps_in_order_and_labels_the_metric(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["sync"] = ExecResult(0, "", "")
    before = _labelled("api")
    # create() already snapshotted the bare template; count only what checkpointing adds.
    before_snapshots = len(host.hypervisor.snapshots)
    ckpt = await checkpoints.create(computer, label="base", pin=True, trigger=CheckpointTrigger.API)
    assert host.guest.commands[-1] == (computer.vm_ip, "sync")
    assert host.hypervisor.snapshots[before_snapshots:] == [
        (computer.socket_path, tmp_path / "ckpts" / ckpt.id)
    ]
    assert host.guest.evicted[-1] == computer.vm_ip
    assert host.blocks.volumes[ckpt.thin_volume_id or -1] == computer.thin_volume_id
    assert host.blocks.active[ckpt.volume_name] == ckpt.thin_volume_id
    assert ckpt.parent_id is None and ckpt.pinned and ckpt.label == "base"
    assert _labelled("api") == before + 1
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    assert sorted(host.objects.prefixes[f"acct-1/{ckpt.id}"]) == ["memory", "vmstate"]


async def test_parent_is_latest_then_source_then_none(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    first = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    second = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    assert first.parent_id is None and second.parent_id == first.id
    fork = await computers.fork(ACCOUNT, second, recipe_id=None)
    third = await checkpoints.create(fork, label=None, trigger=CheckpointTrigger.SELF_DESTRUCT)
    assert third.parent_id == second.id


async def test_delete_cancels_an_in_flight_upload_before_removing_files(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    started = asyncio.Event()
    seen: list[bool] = []

    async def slow_upload(local_dir: Path, prefix: str) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # Records the world as it was at the moment of cancellation, which is
            # the only point at which the two orderings differ.
            seen.append(local_dir.exists())
            raise

    monkeypatch.setattr(host.objects, "upload_dir", slow_upload)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    await started.wait()
    await checkpoints.delete(ckpt)
    assert seen == [True], "the upload was cancelled only after its directory was removed"
    assert len(checkpoints.tasks) == 0, "the upload task was cancelled and reaped"
    assert not (tmp_path / "ckpts" / ckpt.id).exists()
    assert ckpt.thin_volume_id not in host.blocks.volumes
    assert await get_checkpoint(db, ckpt.id) is None


async def test_prune_keeps_the_newest_and_pinned(db: aiosqlite.Connection, tmp_path: Path) -> None:
    checkpoints, computers, _host = await _services(db, tmp_path, retention=2)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ids = []
    for i in range(4):
        ckpt = await checkpoints.create(
            computer, label=None, pin=(i == 0), trigger=CheckpointTrigger.API
        )
        ids.append(ckpt.id)
        await db.execute(
            "UPDATE checkpoints SET created_at = ? WHERE id = ?",
            (f"2026-09-06T00:00:0{i}", ckpt.id),
        )
        await db.commit()
    assert await checkpoints.prune() == 1
    remaining = {c.id for c in await checkpoints.list(ACCOUNT)}
    assert remaining == {ids[0], ids[2], ids[3]}  # pinned oldest survives, unpinned oldest goes


async def test_merge_validates_then_merges_off_loop(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    parent = await checkpoints.create(computer, label="p", trigger=CheckpointTrigger.API)
    fork_a = await computers.fork(ACCOUNT, parent, recipe_id=None)
    fork_b = await computers.fork(ACCOUNT, parent, recipe_id=None)
    a = await checkpoints.create(fork_a, label="a", trigger=CheckpointTrigger.API)
    b = await checkpoints.create(fork_b, label="b", trigger=CheckpointTrigger.API)
    with pytest.raises(BadRequest):
        await checkpoints.merge(ACCOUNT, parent.id, a.id, a.id)
    with pytest.raises(NotFound):
        await checkpoints.merge(ACCOUNT, "ckpt-nope", a.id, b.id)
    with pytest.raises(BadRequest):
        await checkpoints.merge(ACCOUNT, a.id, parent.id, b.id)  # not children of a
    outcome = await checkpoints.merge(ACCOUNT, parent.id, a.id, b.id)
    assert outcome.checkpoint.parent_id == parent.id and outcome.checkpoint.label == "merge"
    assert outcome.conflicts == [] and outcome.checkpoint.thin_volume_id in host.blocks.volumes
    assert host.blocks.volumes[outcome.checkpoint.thin_volume_id or -1] == parent.thin_volume_id


async def test_merge_rejects_missing_foreign_and_diskless_checkpoints(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _host = await _services(db, tmp_path)
    await insert_account(db, OTHER)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    parent = await checkpoints.create(computer, label="p", trigger=CheckpointTrigger.API)
    fork_a = await computers.fork(ACCOUNT, parent, recipe_id=None)
    fork_b = await computers.fork(ACCOUNT, parent, recipe_id=None)
    a = await checkpoints.create(fork_a, label="a", trigger=CheckpointTrigger.API)
    b = await checkpoints.create(fork_b, label="b", trigger=CheckpointTrigger.API)

    with pytest.raises(NotFound, match="Checkpoint A not found"):
        await checkpoints.merge(ACCOUNT, parent.id, "ckpt-nope", b.id)
    with pytest.raises(NotFound, match="Checkpoint B not found"):
        await checkpoints.merge(ACCOUNT, parent.id, a.id, "ckpt-nope")

    # Each of the three operands is checked against the calling account.
    with pytest.raises(NotFound, match="Parent checkpoint not found"):
        await checkpoints.merge(OTHER, parent.id, a.id, b.id)
    foreign = _row("ckpt-foreign", account_id=OTHER.id, parent_id=parent.id, volume_id=901)
    await insert_checkpoint(db, foreign)
    with pytest.raises(NotFound, match="Checkpoint A not found"):
        await checkpoints.merge(ACCOUNT, parent.id, foreign.id, b.id)
    with pytest.raises(NotFound, match="Checkpoint B not found"):
        await checkpoints.merge(ACCOUNT, parent.id, a.id, foreign.id)

    # Owned, a child of the right parent, but never given a disk snapshot.
    diskless = _row("ckpt-diskless", account_id=ACCOUNT.id, parent_id=parent.id, volume_id=None)
    await insert_checkpoint(db, diskless)
    with pytest.raises(BadRequest, match="A checkpoint has no disk snapshot"):
        await checkpoints.merge(ACCOUNT, parent.id, diskless.id, b.id)
    with pytest.raises(BadRequest, match="B checkpoint has no disk snapshot"):
        await checkpoints.merge(ACCOUNT, parent.id, a.id, diskless.id)


async def test_fork_or_defer_honours_exclusive_modes(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ckpt = await checkpoints.create(computer, label="chain", trigger=CheckpointTrigger.API)
    first = await checkpoints.fork_or_defer(ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive=None)
    assert isinstance(first, Computer)
    with pytest.raises(Conflict):
        await checkpoints.fork_or_defer(
            ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="error_on_conflict"
        )
    queued = await checkpoints.fork_or_defer(
        ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="defer_on_conflict"
    )
    assert isinstance(queued, Deferred) and queued.deferred_id.startswith("def-")
    cur = await db.execute("SELECT request_payload FROM deferred_queue WHERE label = 'chain'")
    (payload,) = await cur.fetchone() or ("",)
    assert json.loads(payload) == {
        "checkpoint_id": ckpt.id,
        "recipe_id": None,
        "exec": "echo hi",
        "self_destruct": True,
        "callback_url": None,
        "meta_exec": None,
    }
    await computers.destroy(first.id)
    again = await checkpoints.fork_or_defer(
        ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="error_on_conflict"
    )
    assert isinstance(again, Computer)  # chain is free again
