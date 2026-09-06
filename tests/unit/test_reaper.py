from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from mshkn.config import Config
from mshkn.db import claim_deferred_by_label, get_computer, insert_account, insert_deferred
from mshkn.host import PoolUsage
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Account, Alert, CheckpointTrigger, ComputerStatus
from mshkn.observability.metrics import checkpoints_total, thin_pool_used_ratio
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.reaper import IDLE_LABEL, Reaper
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")


class _Usage:
    def __init__(self, used: int, total: int) -> None:
        self.used, self.total = used, total


async def _reaper(
    db: aiosqlite.Connection, tmp_path: Path, *, idle_timeout: int = 0, disk_pct: float = 10.0
) -> tuple[Reaper, ComputerService, CheckpointService, FakeHostInstance]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "ckpts",
        idle_timeout_seconds=idle_timeout,
    )
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    lifecycle = Lifecycle(db, computers, checkpoints, tasks, httpx.AsyncClient())
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1000 kB\nMemAvailable:    800 kB\n")
    reaper = Reaper(
        config,
        db,
        host,
        computers,
        checkpoints,
        lifecycle,
        deque(maxlen=100),
        disk_usage=lambda _: _Usage(int(disk_pct), 100),
        meminfo_path=meminfo,
    )
    return reaper, computers, checkpoints, host


async def test_dead_vm_is_cleaned_up(db: aiosqlite.Connection, tmp_path: Path) -> None:
    reaper, computers, _, host = await _reaper(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.hypervisor.alive.pop(computer.firecracker_pid or -1)  # the process died
    assert await reaper.reap_dead() == 1
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert host.proxy.routes == {} and host.guest.evicted[-1] == computer.vm_ip
    assert computers.allocator.free_slots == frozenset({computer.slot})


async def test_idle_vm_is_checkpointed_with_trigger_idle_and_its_label_and_drained(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, computers, checkpoints, host = await _reaper(db, tmp_path, idle_timeout=60)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, fork.id))
    await db.commit()
    await insert_deferred(
        db, "def-1", "chain", "acct-1", '{"checkpoint_id": "x", "exec": "echo q"}', "t"
    )
    before = checkpoints_total.labels(trigger="idle")._value.get()
    assert await reaper.reap_idle() == 1
    assert checkpoints_total.labels(trigger="idle")._value.get() == before + 1
    stored = await get_computer(db, fork.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    latest = await checkpoints.latest_for_label(ACCOUNT, "chain")
    assert latest is not None and latest.id != source.id and latest.parent_id == source.id
    await reaper.lifecycle.tasks.drain(timeout=2.0)
    assert await claim_deferred_by_label(db, "chain") == [], "the queue was drained after the reap"
    assert host.hypervisor.alive != {}, "the deferred fork is running"


async def test_idle_vm_without_a_source_gets_the_default_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, computers, checkpoints, _ = await _reaper(db, tmp_path, idle_timeout=60)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, computer.id))
    await db.commit()
    assert await reaper.reap_idle() == 1
    assert (await checkpoints.latest_for_label(ACCOUNT, IDLE_LABEL)) is not None


async def test_recent_exec_keeps_a_vm_alive(db: aiosqlite.Connection, tmp_path: Path) -> None:
    reaper, computers, _, _ = await _reaper(db, tmp_path, idle_timeout=60)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    await db.execute("UPDATE computers SET created_at = ? WHERE id = ?", (stale, computer.id))
    await db.commit()
    await computers.exec(computer, "true")  # touches last_exec_at
    assert await reaper.reap_idle() == 0


async def test_host_checks_raise_pool_alerts_and_set_gauges(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    reaper, _, _, host = await _reaper(db, tmp_path, disk_pct=85.0)
    host.blocks.pool_usage = PoolUsage(data_used_ratio=0.96, metadata_used_ratio=0.5)
    alerts = await reaper.check_host()
    by_source = {a.source: a for a in alerts}
    assert by_source["nvme"].level == "warning"
    assert by_source["thin_pool_data"].level == "critical"
    assert "thin_pool_metadata" not in by_source and "ram" not in by_source
    assert thin_pool_used_ratio.labels(kind="data")._value.get() == 0.96
    assert list(reaper.alerts) == alerts
    assert all(isinstance(a, Alert) for a in alerts)
