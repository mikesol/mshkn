from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_checkpoint, insert_account, insert_checkpoint
from mshkn.errors import BadRequest, Conflict, NotFound
from mshkn.host import ExecResult, SnapshotFiles
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Checkpoint, CheckpointTrigger, Computer, ExecSpec
from mshkn.observability.metrics import checkpoints_total
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService, Deferred
from mshkn.services.computers import ComputerService
from mshkn.services.recipes import RecipeService
from tests.support import account_row, checkpoint_row

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = account_row(api_key="k")
OTHER = account_row(id="acct-2", api_key="k2")
SPEC = ExecSpec(
    command="echo hi", self_destruct=True, callback_url=None, label=None, meta_exec=None
)


async def _services(
    db: aiosqlite.Connection, tmp_path: Path, *, retention: int = 20
) -> tuple[CheckpointService, ComputerService, FakeHostInstance]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "ckpts",
        checkpoint_staging_dir=tmp_path / "staging",
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
        (computer.socket_path, tmp_path / "staging" / ckpt.id)
    ], "Firecracker fsyncs the memory file, so it is written on tmpfs first (#144)"
    assert host.guest.evicted == [], "a 300 ms pause does not break the pooled session (#150)"
    assert host.blocks.volumes[ckpt.thin_volume_id or -1] == computer.thin_volume_id
    assert host.blocks.active[ckpt.volume_name] == ckpt.thin_volume_id
    assert ckpt.parent_id is None and ckpt.pinned and ckpt.label == "base"
    assert _labelled("api") == before + 1
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    assert sorted(host.objects.prefixes[f"acct-1/{ckpt.id}"]) == ["memory", "vmstate"]
    durable = tmp_path / "ckpts" / ckpt.id
    assert (durable / "memory").read_bytes() == b"fake-memory", "persisted before the upload"
    assert not (tmp_path / "ckpts" / f"{ckpt.id}.tmp").exists(), "the copy landed by rename"


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
    foreign = checkpoint_row(
        "ckpt-foreign",
        account_id=OTHER.id,
        computer_id=None,
        parent_id=parent.id,
        thin_volume_id=901,
    )
    await insert_checkpoint(db, foreign)
    with pytest.raises(NotFound, match="Checkpoint A not found"):
        await checkpoints.merge(ACCOUNT, parent.id, foreign.id, b.id)
    with pytest.raises(NotFound, match="Checkpoint B not found"):
        await checkpoints.merge(ACCOUNT, parent.id, a.id, foreign.id)

    # Owned, a child of the right parent, but never given a disk snapshot.
    diskless = checkpoint_row(
        "ckpt-diskless",
        account_id=ACCOUNT.id,
        computer_id=None,
        parent_id=parent.id,
        thin_volume_id=None,
    )
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


async def test_merge_copies_the_result_onto_the_output_volume_in_mount_order(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    parent = await checkpoints.create(computer, label="p", trigger=CheckpointTrigger.API)
    fork_a = await computers.fork(ACCOUNT, parent, recipe_id=None)
    fork_b = await computers.fork(ACCOUNT, parent, recipe_id=None)
    a = await checkpoints.create(fork_a, label="a", trigger=CheckpointTrigger.API)
    b = await checkpoints.create(fork_b, label="b", trigger=CheckpointTrigger.API)
    # seed the three "disks" through the fake's stable mounts
    async with host.blocks.mounted(parent.volume_name) as mp:
        (mp / "base.txt").write_text("v0")
        (mp / "doomed.txt").write_text("bye")
        (mp / "conflict.txt").write_text("v0")
    async with host.blocks.mounted(a.volume_name) as ma:
        (ma / "base.txt").write_text("v0")
        (ma / "conflict.txt").write_text("A")
        (ma / "a_only.txt").write_text("a")
    async with host.blocks.mounted(b.volume_name) as mb:
        (mb / "base.txt").write_text("v1")
        (mb / "doomed.txt").write_text("bye")
        (mb / "conflict.txt").write_text("B")
    host.blocks.calls.clear()
    outcome = await checkpoints.merge(ACCOUNT, parent.id, a.id, b.id)
    assert outcome.conflicts == ["conflict.txt"]
    assert outcome.auto_merged == 3
    mounts = [args for name, args in host.blocks.calls if name == "mounted"]
    assert mounts == [
        (parent.volume_name, True),
        (a.volume_name, True),
        (b.volume_name, True),
        (outcome.checkpoint.volume_name, False),
    ]
    out = host.blocks.mounts[outcome.checkpoint.volume_name]
    assert (out / "base.txt").read_text() == "v1"
    assert (out / "conflict.txt").read_text() == "A"
    assert (out / "a_only.txt").read_text() == "a"
    # doomed.txt is deleted in A and unchanged in B, so the algorithm drops it. The
    # output volume began as a copy of the parent, which had it, so only the
    # copy-back's deletion loop can take it off; this is the sole test of that loop.
    # (That the copy happens at all is pinned by test_snap_copies_the_source_volume_content.)
    assert not (out / "doomed.txt").exists(), "the copy-back must delete what the merge dropped"
    host.close()


async def _chain(
    checkpoints: CheckpointService, computers: ComputerService, *labels: str
) -> list[Checkpoint]:
    """One base computer checkpointed once per label, in order, then destroyed."""
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    made = [
        await checkpoints.create(base, label=label, trigger=CheckpointTrigger.API)
        for label in labels
    ]
    await computers.destroy(base.id)
    return made


async def test_fork_by_label_forks_the_newest_checkpoint_carrying_the_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _host = await _services(db, tmp_path)
    _old, new, _other = await _chain(checkpoints, computers, "chain", "chain", "other")

    head, forked = await checkpoints.fork_by_label(
        ACCOUNT, "chain", SPEC, exclusive=None, recipe_id=None
    )

    assert head.id == new.id
    assert isinstance(forked, Computer) and forked.source_checkpoint_id == new.id


async def test_fork_by_label_is_404_for_an_unknown_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _ = await _services(db, tmp_path)
    await _chain(checkpoints, computers, "chain")
    with pytest.raises(NotFound, match="No checkpoint with label 'missing'"):
        await checkpoints.fork_by_label(ACCOUNT, "missing", SPEC, exclusive=None, recipe_id=None)


async def test_fork_by_label_does_not_see_another_accounts_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _ = await _services(db, tmp_path)
    await _chain(checkpoints, computers, "chain")
    await insert_account(db, OTHER)
    with pytest.raises(NotFound):
        await checkpoints.fork_by_label(OTHER, "chain", SPEC, exclusive=None, recipe_id=None)


async def test_two_concurrent_exclusive_forks_by_label_admit_exactly_one(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """Both callers pass the active-computer check before either has inserted a
    computer row unless admission is serialised; the lock makes the second see
    the first's computer and get the conflict."""
    checkpoints, computers, host = await _services(db, tmp_path)
    await _chain(checkpoints, computers, "chain")
    restores_before = len(host.hypervisor.restored)

    results = await asyncio.gather(
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="error_on_conflict", recipe_id=None
        ),
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="error_on_conflict", recipe_id=None
        ),
        return_exceptions=True,
    )

    forked = [r for r in results if isinstance(r, tuple)]
    conflicts = [r for r in results if isinstance(r, Conflict)]
    assert len(forked) == 1 and len(conflicts) == 1, results
    assert len(host.hypervisor.restored) - restores_before == 1


async def test_two_concurrent_deferring_forks_by_label_fork_one_and_queue_one(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    (ckpt,) = await _chain(checkpoints, computers, "chain")
    restores_before = len(host.hypervisor.restored)

    results = await asyncio.gather(
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="defer_on_conflict", recipe_id=None
        ),
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="defer_on_conflict", recipe_id=None
        ),
    )

    outcomes = [outcome for _, outcome in results]
    assert sum(isinstance(o, Computer) for o in outcomes) == 1
    assert sum(isinstance(o, Deferred) for o in outcomes) == 1
    assert len(host.hypervisor.restored) - restores_before == 1
    cur = await db.execute("SELECT request_payload FROM deferred_queue WHERE label = 'chain'")
    rows = list(await cur.fetchall())
    assert len(rows) == 1 and json.loads(rows[0][0])["checkpoint_id"] == ckpt.id


async def test_fork_by_id_with_exclusive_contends_on_the_label_lock(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    (ckpt,) = await _chain(checkpoints, computers, "chain")
    restores_before = len(host.hypervisor.restored)

    results = await asyncio.gather(
        checkpoints.fork_or_defer(
            ACCOUNT, ckpt, SPEC, recipe_id=None, exclusive="error_on_conflict"
        ),
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="error_on_conflict", recipe_id=None
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(r, Conflict) for r in results) == 1, results
    assert len(host.hypervisor.restored) - restores_before == 1


async def test_labels_lock_independently(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """Two labels, and the same label on two accounts, do not wait on each other."""
    checkpoints, computers, host = await _services(db, tmp_path)
    await _chain(checkpoints, computers, "a", "b")
    await insert_account(db, OTHER)
    base = await computers.create(OTHER, recipe_id=None, resources=DEFAULT_RESOURCES)
    await checkpoints.create(base, label="a", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    restores_before = len(host.hypervisor.restored)

    results = await asyncio.gather(
        checkpoints.fork_by_label(
            ACCOUNT, "a", SPEC, exclusive="error_on_conflict", recipe_id=None
        ),
        checkpoints.fork_by_label(
            ACCOUNT, "b", SPEC, exclusive="error_on_conflict", recipe_id=None
        ),
        checkpoints.fork_by_label(OTHER, "a", SPEC, exclusive="error_on_conflict", recipe_id=None),
    )

    assert all(isinstance(outcome, Computer) for _, outcome in results)
    assert len(host.hypervisor.restored) - restores_before == 3


async def test_label_locks_exist_only_while_held(db: aiosqlite.Connection, tmp_path: Path) -> None:
    """A lock entry lives from the first waiter to the last release; a label
    that 404s or a chain that is idle leaves nothing behind."""
    checkpoints, computers, _host = await _services(db, tmp_path)
    await _chain(checkpoints, computers, "chain")
    with pytest.raises(NotFound):
        await checkpoints.fork_by_label(ACCOUNT, "missing", SPEC, exclusive=None, recipe_id=None)
    assert checkpoints._label_locks == {}

    await asyncio.gather(
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="defer_on_conflict", recipe_id=None
        ),
        checkpoints.fork_by_label(
            ACCOUNT, "chain", SPEC, exclusive="defer_on_conflict", recipe_id=None
        ),
    )
    assert checkpoints._label_locks == {}


async def test_create_snaps_the_disk_only_after_the_memory_snapshot_returned(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dm-thin snap waits for the memory snapshot; overlapping them is wrong.

    Firecracker's drive cache is Unsafe: the guest's `sync` never reaches the
    host disk, and it is Firecracker's own flush inside create_snapshot that
    lands the guest's writes on the thin volume. A snap taken during the dump
    captured empty files on the live host (the #147 overlap, reverted).
    """
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    real_snapshot = host.hypervisor.snapshot
    volumes_during_dump: list[set[int]] = []

    async def slow_snapshot(socket_path: str, dest_dir: Path) -> SnapshotFiles:
        await asyncio.sleep(0.01)  # the dump takes a while; the snap should land meanwhile
        volumes_during_dump.append(set(host.blocks.volumes))
        return await real_snapshot(socket_path, dest_dir)

    monkeypatch.setattr(host.hypervisor, "snapshot", slow_snapshot)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    assert ckpt.thin_volume_id not in volumes_during_dump[0], (
        "the disk snap must not run until Firecracker has flushed the drive"
    )
    assert ckpt.thin_volume_id in host.blocks.volumes


async def test_the_staging_copy_goes_after_a_linger_and_delete_clears_both(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tmpfs copy outlives the upload by a linger, so a fork that resolved
    the staging path a moment before the durable copy appeared still finds its
    files; delete clears both copies at once."""
    monkeypatch.setattr("mshkn.services.checkpoints._STAGING_LINGER_SECONDS", 0.0)
    checkpoints, computers, _host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    staging = tmp_path / "staging" / ckpt.id
    assert staging.is_dir()
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    await checkpoints.tasks.wait(checkpoints.staging_clear_task_key(ckpt.id))
    assert not staging.exists(), "the staging copy is released once it is safe"
    assert (tmp_path / "ckpts" / ckpt.id / "memory").exists()
    other = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    await checkpoints.tasks.wait(checkpoints.upload_task_key(other.id))
    await checkpoints.delete(other)
    assert not (tmp_path / "staging" / other.id).exists()
    assert not (tmp_path / "ckpts" / other.id).exists()
    assert checkpoints.staging_clear_task_key(other.id) not in checkpoints.tasks.names(), (
        "delete cancels the linger too, so nothing of the checkpoint outlives it"
    )


async def test_uploads_run_one_at_a_time(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploads of 256 MiB files ran for minutes, several at once, against the
    same disk the next checkpoint fsyncs to (#145); one at a time bounds that."""
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    in_flight = 0
    peak = 0

    async def slow_upload(local_dir: Path, prefix: str) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1

    monkeypatch.setattr(host.objects, "upload_dir", slow_upload)
    made = [
        await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
        for _ in range(3)
    ]
    for ckpt in made:
        await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    assert peak == 1


async def test_a_failed_persist_still_uploads_from_staging_and_keeps_it(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mshkn.services.checkpoints._STAGING_LINGER_SECONDS", 0.0)
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    (tmp_path / "ckpts").mkdir(exist_ok=True)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    # The durable path is unusable: a file sits where the checkpoint's directory goes.
    (tmp_path / "ckpts" / f"{ckpt.id}.tmp").write_text("in the way")
    (tmp_path / "ckpts" / ckpt.id).write_text("in the way")
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    await checkpoints.tasks.wait(checkpoints.staging_clear_task_key(ckpt.id))
    assert sorted(host.objects.prefixes[f"acct-1/{ckpt.id}"]) == ["memory", "vmstate"]
    assert (tmp_path / "staging" / ckpt.id / "memory").exists(), "the only copy is kept"


async def test_the_staging_copy_is_released_once_the_durable_copy_exists(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The linger starts when the durable copy lands, not when the upload ends.

    Uploads run one at a time and take tens of seconds each, so freeing tmpfs
    only after the upload let staging copies pile up until /dev/shm was full
    and Firecracker failed with ENOSPC on the live host.
    """
    monkeypatch.setattr("mshkn.services.checkpoints._STAGING_LINGER_SECONDS", 0.0)
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    upload_started = asyncio.Event()
    release_upload = asyncio.Event()

    async def blocked_upload(local_dir: Path, prefix: str) -> None:
        upload_started.set()
        await release_upload.wait()

    monkeypatch.setattr(host.objects, "upload_dir", blocked_upload)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    await upload_started.wait()
    await checkpoints.tasks.wait(checkpoints.staging_clear_task_key(ckpt.id))
    assert not (tmp_path / "staging" / ckpt.id).exists(), "released while the upload still runs"
    assert (tmp_path / "ckpts" / ckpt.id / "memory").exists()
    release_upload.set()
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))


async def test_create_falls_back_to_the_durable_dir_when_the_staging_write_fails(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """A full tmpfs must not cost the checkpoint: the snapshot is retried onto disk."""
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.hypervisor.fail_next("snapshot")
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    assert host.hypervisor.snapshots[-1] == (
        computer.socket_path,
        tmp_path / "ckpts" / ckpt.id,
    ), "the retry wrote straight into the durable directory"
    assert not (tmp_path / "staging" / ckpt.id).exists()
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))
    assert sorted(host.objects.prefixes[f"acct-1/{ckpt.id}"]) == ["memory", "vmstate"]
    assert checkpoints.staging_clear_task_key(ckpt.id) not in checkpoints.tasks.names()
