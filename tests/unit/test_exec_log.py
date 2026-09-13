"""Exec output of an ephemeral run outlives the computer (#58): every run_ephemeral
with a command writes one exec_log row keyed by computer id, bounded in size,
linked to the checkpoint it produced, readable by its owner, and expired by the
reaper after the configured retention."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from mshkn.config import Config
from mshkn.db import (
    delete_exec_logs_before,
    get_exec_log,
    insert_account,
    insert_exec_log,
    set_exec_log_checkpoint,
)
from mshkn.errors import NotFound
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import CheckpointTrigger, ExecLog, ExecSpec
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import EXEC_LOG_OUTPUT_BYTES, Lifecycle, truncate_output
from mshkn.services.reaper import Reaper
from mshkn.services.recipes import RecipeService
from tests.support import account_row

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = account_row(api_key="k")
OTHER = account_row(id="acct-2", api_key="k2")


def _row(computer_id: str = "comp-1", *, created_at: str = "2026-09-08T00:00:00+00:00") -> ExecLog:
    return ExecLog(
        computer_id=computer_id,
        account_id="acct-1",
        source_checkpoint_id=None,
        created_checkpoint_id=None,
        label="chain",
        command="echo hi",
        exit_code=0,
        stdout="hi\n",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        created_at=created_at,
    )


async def _services(
    db: aiosqlite.Connection, tmp_path: Path, *, retention: int = 86400
) -> tuple[Lifecycle, ComputerService, CheckpointService, FakeHostInstance, Reaper]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "ckpts",
        checkpoint_staging_dir=tmp_path / "staging",
        idle_timeout_seconds=0,
        exec_log_retention_seconds=retention,
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
        disk_usage=lambda _: type("U", (), {"used": 10, "total": 100})(),
        meminfo_path=meminfo,
    )
    return lifecycle, computers, checkpoints, host, reaper


# -- the table -----------------------------------------------------------------


async def test_exec_log_roundtrip_and_checkpoint_link(db: aiosqlite.Connection) -> None:
    await insert_exec_log(db, _row())
    assert await get_exec_log(db, "comp-1") == _row()
    await set_exec_log_checkpoint(db, "comp-1", "ckpt-9")
    stored = await get_exec_log(db, "comp-1")
    assert stored is not None and stored.created_checkpoint_id == "ckpt-9"
    assert await get_exec_log(db, "comp-none") is None


async def test_delete_exec_logs_before_removes_only_older_rows(db: aiosqlite.Connection) -> None:
    await insert_exec_log(db, _row("comp-old", created_at="2026-09-01T00:00:00+00:00"))
    await insert_exec_log(db, _row("comp-new", created_at="2026-09-08T00:00:00+00:00"))
    assert await delete_exec_logs_before(db, "2026-09-07T00:00:00+00:00") == 1
    assert await get_exec_log(db, "comp-old") is None
    assert await get_exec_log(db, "comp-new") is not None


# -- truncation ----------------------------------------------------------------


def test_truncate_output_keeps_short_text_whole() -> None:
    assert truncate_output("abc", 10) == ("abc", False)


def test_truncate_output_keeps_the_head_and_the_tail_and_says_what_went() -> None:
    text = "".join(f"line {i:04d}\n" for i in range(1000))  # 10 000 bytes
    kept, truncated = truncate_output(text, 1000)
    assert truncated
    assert kept.startswith("line 0000\n")
    assert kept.endswith("line 0999\n")
    assert "[mshkn: 9000 bytes truncated]" in kept
    assert len(kept.encode()) < 1100


def test_truncate_output_cuts_on_bytes_without_breaking_characters() -> None:
    text = "é" * 100  # 200 bytes
    kept, truncated = truncate_output(text, 51)  # 25 bytes a side: 12 whole characters
    assert truncated and "�" not in kept
    assert kept.startswith("é" * 12 + "\n") and kept.endswith("\n" + "é" * 12)


def test_default_limit_is_eight_kib() -> None:
    assert EXEC_LOG_OUTPUT_BYTES == 8192


# -- run_ephemeral records it --------------------------------------------------


async def test_self_destruct_run_is_recorded_with_its_checkpoint(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _services(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    host.guest.script["python brain.py"] = ExecResult(3, "Called lampas: 502\n", "boom\n")
    spec = ExecSpec(
        command="python brain.py",
        self_destruct=True,
        callback_url=None,
        label=None,
        meta_exec=None,
    )
    result = await lifecycle.run_ephemeral(ACCOUNT, fork, spec, source_checkpoint=source)
    log = await lifecycle.exec_log(ACCOUNT, fork.id)
    assert log.command == "python brain.py"
    assert (log.exit_code, log.stdout, log.stderr) == (3, "Called lampas: 502\n", "boom\n")
    assert (log.stdout_truncated, log.stderr_truncated) == (False, False)
    assert log.source_checkpoint_id == source.id and log.label == "chain"
    assert log.created_checkpoint_id == result.created_checkpoint_id
    datetime.fromisoformat(log.created_at)  # a real timestamp


async def test_a_run_that_keeps_the_computer_is_recorded_without_a_checkpoint(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, _, host, _ = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["true"] = ExecResult(0, "", "")
    spec = ExecSpec(
        command="true", self_destruct=False, callback_url=None, label="x", meta_exec=None
    )
    await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    log = await lifecycle.exec_log(ACCOUNT, computer.id)
    assert log.created_checkpoint_id is None and log.source_checkpoint_id is None
    assert log.label == "x"


async def test_no_command_means_no_record(db: aiosqlite.Connection, tmp_path: Path) -> None:
    lifecycle, computers, _, _, _ = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    spec = ExecSpec(command=None, self_destruct=True, callback_url=None, label=None, meta_exec=None)
    await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    with pytest.raises(NotFound):
        await lifecycle.exec_log(ACCOUNT, computer.id)


async def test_stored_output_is_bounded_but_the_response_is_not(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, _, host, _ = await _services(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    big = "x" * (EXEC_LOG_OUTPUT_BYTES * 3)
    host.guest.script["yes"] = ExecResult(0, big, big)
    spec = ExecSpec(
        command="yes", self_destruct=False, callback_url=None, label=None, meta_exec=None
    )
    result = await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    assert result.exec_stdout == big and result.exec_stderr == big
    log = await lifecycle.exec_log(ACCOUNT, computer.id)
    assert log.stdout_truncated and log.stderr_truncated
    assert len(log.stdout.encode()) <= EXEC_LOG_OUTPUT_BYTES + 64
    assert len(log.stderr.encode()) <= EXEC_LOG_OUTPUT_BYTES + 64


async def test_exec_log_is_the_owners_only(db: aiosqlite.Connection, tmp_path: Path) -> None:
    lifecycle, computers, _, host, _ = await _services(db, tmp_path)
    await insert_account(db, OTHER)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["true"] = ExecResult(0, "", "")
    spec = ExecSpec(
        command="true", self_destruct=True, callback_url=None, label=None, meta_exec=None
    )
    await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    with pytest.raises(NotFound):
        await lifecycle.exec_log(OTHER, computer.id)
    with pytest.raises(NotFound):
        await lifecycle.exec_log(ACCOUNT, "comp-missing")


# -- retention -----------------------------------------------------------------


async def test_reaper_cycle_expires_exec_logs_past_retention(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, _, _, _, reaper = await _services(db, tmp_path, retention=3600)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await insert_exec_log(db, _row("comp-old", created_at=old))
    await insert_exec_log(db, _row("comp-fresh", created_at=fresh))
    assert await lifecycle.expire_exec_logs() == 1
    assert await get_exec_log(db, "comp-old") is None
    assert await get_exec_log(db, "comp-fresh") is not None
    await reaper.cycle()  # the cycle calls it too, and a second pass finds nothing
    assert await get_exec_log(db, "comp-fresh") is not None


async def test_retention_of_zero_keeps_everything(db: aiosqlite.Connection, tmp_path: Path) -> None:
    lifecycle, _, _, _, _ = await _services(db, tmp_path, retention=0)
    await insert_exec_log(db, _row("comp-old", created_at="2020-01-01T00:00:00+00:00"))
    assert await lifecycle.expire_exec_logs() == 0
    assert await get_exec_log(db, "comp-old") is not None
