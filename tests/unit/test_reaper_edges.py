"""Reaper failure and threshold branches the happy-path reaper tests do not reach."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NoReturn

from mshkn.db import get_computer, insert_account
from mshkn.host import PoolUsage
from mshkn.models import ComputerStatus
from mshkn.resources import DEFAULT_RESOURCES
from tests.support import account_row
from tests.unit.test_reaper import ACCOUNT, _reaper

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite
    import pytest

    from mshkn.models import Computer


async def test_run_loop_survives_a_failing_cycle(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reaper, _, _, _ = await _reaper(db, tmp_path)
    calls = 0

    async def cycle() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(reaper, "cycle", cycle)
    task = asyncio.create_task(reaper.run(interval=0.01))
    await asyncio.sleep(0.08)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls >= 2  # the loop kept going after the first cycle raised
    assert any("Reaper cycle failed" in r.getMessage() for r in caplog.records)


async def test_reap_dead_isolates_a_failing_cleanup(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reaper, computers, _, host = await _reaper(db, tmp_path)
    a = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    b = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.hypervisor.alive.clear()  # both processes died
    original = computers.cleanup_dead

    async def cleanup(computer: Computer) -> None:
        if computer.id == a.id:
            raise RuntimeError("stuck")
        await original(computer)

    monkeypatch.setattr(computers, "cleanup_dead", cleanup)

    assert await reaper.reap_dead() == 1
    stored = await get_computer(db, b.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert any(f"Failed to reap VM {a.id}" in r.getMessage() for r in caplog.records)


async def test_idle_skips_unparseable_timestamps_and_destroys_when_the_checkpoint_fails(
    db: aiosqlite.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reaper, computers, _, host = await _reaper(db, tmp_path, idle_timeout=60)
    bad = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await db.execute("UPDATE computers SET created_at = 'not-a-date' WHERE id = ?", (bad.id,))
    stale = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    # Naive, as rows written before the timestamps became tz-aware are.
    naive = (datetime.now(UTC) - timedelta(seconds=120)).replace(tzinfo=None).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (naive, stale.id))
    await db.commit()
    host.hypervisor.fail_next("snapshot")

    assert await reaper.reap_idle() == 1

    unparseable = await get_computer(db, bad.id)
    destroyed = await get_computer(db, stale.id)
    assert unparseable is not None and unparseable.status is ComputerStatus.RUNNING
    assert destroyed is not None and destroyed.status is ComputerStatus.DESTROYED
    assert any("Auto-checkpoint failed" in r.getMessage() for r in caplog.records)


async def test_idle_isolates_a_reap_that_raises(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reaper, computers, _, _ = await _reaper(db, tmp_path, idle_timeout=60)
    stuck = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, stuck.id))
    await db.commit()

    async def destroy(computer_id: str) -> None:
        raise RuntimeError("teardown wedged")

    monkeypatch.setattr(computers, "destroy", destroy)

    assert await reaper.reap_idle() == 0
    assert any(f"Failed to reap idle VM {stuck.id}" in r.getMessage() for r in caplog.records)


async def test_host_checks_cover_every_threshold(db: aiosqlite.Connection, tmp_path: Path) -> None:
    reaper, _, _, host = await _reaper(db, tmp_path, disk_pct=96.0)
    # The blank line exercises the meminfo parser's "not a key/value line" path.
    (tmp_path / "meminfo").write_text("MemTotal:       1000 kB\n\nMemAvailable:    50 kB\n")
    host.blocks.pool_usage = PoolUsage(data_used_ratio=0.85, metadata_used_ratio=0.97)

    by_source = {a.source: a for a in await reaper.check_host()}

    assert by_source["nvme"].level == "critical"
    assert by_source["ram"].level == "critical"
    assert by_source["thin_pool_data"].level == "warning"
    assert by_source["thin_pool_metadata"].level == "critical"


async def test_meminfo_without_a_total_yields_no_ram_alert(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, _, _, _ = await _reaper(db, tmp_path)
    (tmp_path / "meminfo").write_text("MemAvailable:    50 kB\n")
    assert [a.source for a in await reaper.check_host()] == []


async def test_host_checks_log_and_continue_when_a_probe_raises(
    db: aiosqlite.Connection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaper, _, _, host = await _reaper(db, tmp_path)
    (tmp_path / "meminfo").unlink()

    def disk_usage(path: str) -> NoReturn:
        raise RuntimeError("statvfs failed")

    async def usage() -> PoolUsage:
        raise RuntimeError("dmsetup missing")

    monkeypatch.setattr(reaper, "_disk_usage", disk_usage)
    monkeypatch.setattr(host.blocks, "usage", usage)

    assert await reaper.check_host() == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("Failed to check disk usage" in m for m in messages)
    assert any("Failed to check RAM usage" in m for m in messages)
    assert any("Failed to check thin pool usage" in m for m in messages)


async def test_a_cycle_with_nothing_to_do_does_not_report_one(
    db: aiosqlite.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reaper, _, _, _ = await _reaper(db, tmp_path)
    await reaper.cycle()
    assert not any("Reaper cycle:" in r.getMessage() for r in caplog.records)


async def test_idle_reap_of_an_orphaned_account_skips_the_drain(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """The account row can be gone by the time the reaper gets to its VM."""
    reaper, computers, _, _ = await _reaper(db, tmp_path, idle_timeout=60)
    orphan_account = account_row("acct-gone", api_key="k-gone")
    await insert_account(db, orphan_account)
    computer = await computers.create(orphan_account, recipe_id=None, resources=DEFAULT_RESOURCES)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, computer.id))
    await db.execute("DELETE FROM accounts WHERE id = ?", (orphan_account.id,))
    await db.commit()

    assert await reaper.reap_idle() == 1
    assert not any(t.get_name().startswith("deferred:") for t in reaper.lifecycle.tasks._tasks)
