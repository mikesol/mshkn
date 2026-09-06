"""Deferred-drain branches the happy-path lifecycle tests do not reach."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mshkn.db import claim_deferred_by_label, insert_deferred, list_all_computers
from mshkn.models import CheckpointTrigger, ComputerStatus
from mshkn.resources import DEFAULT_RESOURCES
from tests.unit.test_lifecycle import ACCOUNT, _lifecycle

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite
    import pytest


async def test_drain_with_no_command_leaves_the_fork_running(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    await insert_deferred(
        db, "def-0", "chain", ACCOUNT.id, json.dumps({"checkpoint_id": source.id}), "t"
    )

    await lifecycle.drain_deferred(ACCOUNT, "chain")

    running = [c for c in await list_all_computers(db) if c.status is ComputerStatus.RUNNING]
    assert len(running) == 1
    assert running[0].source_checkpoint_id == source.id
    # The exec files are still written; nothing is run on top of them.
    assert [cmd for _, cmd in host.guest.commands] == [
        "sync",
        "mkdir -p /tmp/exec && printf '%s' '' > /tmp/exec/0.txt",
    ]


async def test_drain_logs_and_swallows_a_fork_failure(
    db: aiosqlite.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    await insert_deferred(
        db,
        "def-0",
        "chain",
        ACCOUNT.id,
        json.dumps({"checkpoint_id": source.id, "exec": "true"}),
        "t",
    )
    host.hypervisor.fail_next("restore")

    await lifecycle.drain_deferred(ACCOUNT, "chain")  # no raise

    assert any(
        "Failed to process deferred queue for label chain" in r.getMessage() for r in caplog.records
    )
    # The claim is a DELETE … RETURNING, so a failed batch is consumed, not requeued.
    assert await claim_deferred_by_label(db, "chain") == []
