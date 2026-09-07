"""ComputerService and CheckpointService branches the main service tests do not reach.

These pin the behaviour that only shows up when a host call fails, when a row
is missing a volume, or when a caller reaches for someone else's id.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mshkn.db import get_computer, insert_account, insert_checkpoint
from mshkn.errors import Conflict, HostError, NotFound
from mshkn.host import ExecResult
from mshkn.models import CheckpointTrigger, Computer, ComputerStatus, ExecSpec
from mshkn.resources import DEFAULT_RESOURCES
from tests.support import account_row, checkpoint_row
from tests.unit.test_checkpoint_service import ACCOUNT, _services

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

    from mshkn.models import Checkpoint

OTHER = account_row("acct-2", api_key="k2")
SPEC = ExecSpec(command=None, self_destruct=False, callback_url=None, label=None, meta_exec=None)


def _bare_checkpoint(r2_prefix: str) -> Checkpoint:
    """A checkpoint over volume 0 (the base image) with no local snapshot files."""
    ckpt = checkpoint_row("ckpt-bare", computer_id=None, thin_volume_id=0)
    return replace(ckpt, r2_prefix=r2_prefix)


# -- ComputerService -------------------------------------------------------


async def test_get_owned_hides_another_accounts_computer(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, _ = await _services(db, tmp_path)
    mine = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await insert_account(db, OTHER)
    with pytest.raises(NotFound):
        await computers.get_owned(OTHER, mine.id)


async def test_exec_kill_passes_a_non_zero_result_through(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["kill 42"] = ExecResult(exit_code=1, stdout="", stderr="no such process")

    result = await computers.exec_kill(computer, 42)

    assert (result.exit_code, result.stderr) == (1, "no such process")


async def test_exec_logs_splits_the_log_into_lines(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    command = "cat /tmp/bg-7.log 2>/dev/null || echo ''"
    host.guest.script[command] = ExecResult(exit_code=0, stdout="one\ntwo\n", stderr="")

    assert await computers.exec_logs(computer, 7) == ["one", "two"]


async def test_metrics_returns_none_and_warns_when_the_guest_fails(
    db: aiosqlite.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.fail_next("metrics")

    assert await computers.metrics(computer) is None
    assert any(
        f"Failed to gather metrics for {computer.id}" in r.getMessage() for r in caplog.records
    )


async def test_forking_a_checkpoint_without_a_disk_snapshot_conflicts(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, _ = await _services(db, tmp_path)
    ckpt = checkpoint_row("ckpt-nodisk", computer_id=None, thin_volume_id=None)
    await insert_checkpoint(db, ckpt)
    with pytest.raises(Conflict, match="no disk snapshot"):
        await computers.fork(ACCOUNT, ckpt, recipe_id=None)


async def test_forking_a_checkpoint_with_no_r2_prefix_cold_boots(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    source = _bare_checkpoint(r2_prefix="")
    await insert_checkpoint(db, source)

    computer = await computers.fork(ACCOUNT, source, recipe_id=None)

    assert host.hypervisor.restored == []
    assert host.hypervisor.booted[-1][0] == computer.thin_volume_id


async def test_forking_a_checkpoint_whose_download_fails_cold_boots(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    source = _bare_checkpoint(r2_prefix="acct-1/gone")
    await insert_checkpoint(db, source)
    host.objects.fail_next("download_dir")

    computer = await computers.fork(ACCOUNT, source, recipe_id=None)

    assert host.hypervisor.restored == []
    assert host.hypervisor.booted[-1][0] == computer.thin_volume_id


async def test_a_failing_disk_snap_hands_the_slot_back(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    host.blocks.fail_next("snap")

    with pytest.raises(HostError, match="snap"):
        await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    assert computers.allocator.free_slots == frozenset({1})


async def test_a_non_domain_failure_during_bring_up_becomes_a_host_error(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, computers, host = await _services(db, tmp_path)

    async def warm(vm_ip: str) -> None:
        raise ValueError("ssh handshake garbled")

    monkeypatch.setattr(host.guest, "warm", warm)

    with pytest.raises(HostError, match="ValueError: ssh handshake garbled"):
        await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    assert computers.allocator.free_slots == frozenset({1})
    assert host.proxy.routes == {}


async def test_abandon_clears_a_route_a_failing_add_left_behind(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_route can apply the route and then fail; abandon has to undo it.

    The fake fails add_route before it records anything, so fail_next cannot
    produce this state; only a proxy that half-applies can.
    """
    _, computers, host = await _services(db, tmp_path)
    original = host.proxy.add_route

    async def half_applied(computer_id: str, vm_ip: str) -> None:
        await original(computer_id, vm_ip)
        raise RuntimeError("caddy accepted then reset")

    monkeypatch.setattr(host.proxy, "add_route", half_applied)

    with pytest.raises(HostError):
        await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    assert host.proxy.routes == {}


async def test_destroy_of_a_computer_without_a_pid_or_ip_still_releases_it(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await db.execute(
        "UPDATE computers SET firecracker_pid = NULL, vm_ip = '' WHERE id = ?", (computer.id,)
    )
    await db.commit()
    evicted_before = list(host.guest.evicted)

    await computers.destroy(computer.id)

    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert host.guest.evicted == evicted_before  # nothing to evict without an IP


async def test_cleanup_dead_swallows_teardown_and_eviction_failures(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("device busy")

    monkeypatch.setattr(host.hypervisor, "teardown_slot", boom)
    monkeypatch.setattr(host.guest, "evict", boom)

    await computers.cleanup_dead(computer)  # no raise

    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED


# -- CheckpointService -----------------------------------------------------


async def test_checkpoint_get_owned_hides_another_accounts_checkpoint(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _ = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    await insert_account(db, OTHER)
    with pytest.raises(NotFound):
        await checkpoints.get_owned(OTHER, ckpt.id)


async def test_deleting_a_checkpoint_without_a_volume_removes_nothing(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, _, host = await _services(db, tmp_path)
    ckpt = checkpoint_row("ckpt-nodisk", computer_id=None, thin_volume_id=None)
    await insert_checkpoint(db, ckpt)

    await checkpoints.delete(ckpt)

    assert not any(name == "remove" for name, _ in host.blocks.calls)


async def test_an_upload_failure_is_logged_not_raised(
    db: aiosqlite.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    checkpoints, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.objects.fail_next("upload_dir")

    ckpt = await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    await checkpoints.tasks.wait(checkpoints.upload_task_key(ckpt.id))

    assert any(
        f"R2 upload failed for checkpoint {ckpt.id}" in r.getMessage() for r in caplog.records
    )


async def test_prune_is_off_when_retention_is_zero(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, computers, _ = await _services(db, tmp_path, retention=0)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await checkpoints.create(computer, label=None, trigger=CheckpointTrigger.API)
    assert await checkpoints.prune() == 0


async def test_prune_isolates_a_delete_that_fails(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    checkpoints, _, _ = await _services(db, tmp_path, retention=1)
    for n, created in ((1, "2026-03-01T00:00:00"), (2, "2026-03-02T00:00:00")):
        await insert_checkpoint(
            db,
            checkpoint_row(
                f"ckpt-{n}", computer_id=None, thin_volume_id=50 + n, created_at=created
            ),
        )
    await insert_checkpoint(
        db, checkpoint_row("ckpt-keep", computer_id=None, created_at="2026-03-03T00:00:00")
    )
    original = checkpoints.delete

    async def delete(checkpoint: Checkpoint) -> None:
        if checkpoint.id == "ckpt-1":
            raise RuntimeError("volume busy")
        await original(checkpoint)

    monkeypatch.setattr(checkpoints, "delete", delete)

    assert await checkpoints.prune() == 1
    assert any("Failed to prune checkpoint ckpt-1" in r.getMessage() for r in caplog.records)


async def test_exclusive_on_an_unlabelled_checkpoint_forks_straight_away(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    checkpoints, _, _ = await _services(db, tmp_path)
    source = _bare_checkpoint(r2_prefix="")
    await insert_checkpoint(db, source)

    outcome = await checkpoints.fork_or_defer(
        ACCOUNT, source, SPEC, recipe_id=None, exclusive="defer_on_conflict"
    )

    assert isinstance(outcome, Computer)
    assert outcome.source_checkpoint_id == source.id


async def test_cleanup_dead_of_a_computer_without_an_ip_evicts_nothing(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    _, computers, host = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    evicted_before = list(host.guest.evicted)

    await computers.cleanup_dead(replace(computer, vm_ip=""))

    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert host.guest.evicted == evicted_before
