from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request

from mshkn.config import Config
from mshkn.db import (
    claim_deferred_by_label,
    delete_checkpoint,
    get_computer,
    insert_account,
    insert_checkpoint,
    insert_deferred,
    insert_recipe,
    list_all_computers,
)
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import (
    Account,
    CheckpointTrigger,
    ComputerStatus,
    ExecSpec,
    Recipe,
    RecipeStatus,
)
from mshkn.observability.metrics import checkpoints_total
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.checkpoints import CheckpointService
from mshkn.services.computers import ComputerService
from mshkn.services.lifecycle import Lifecycle
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=10, created_at="t")
OTHER = Account(id="acct-2", api_key="k2", vm_limit=10, created_at="t")


def _ready_recipe(recipe_id: str) -> Recipe:
    return Recipe(
        id=recipe_id,
        account_id=ACCOUNT.id,
        dockerfile="FROM debian",
        content_hash=f"hash-{recipe_id}",
        status=RecipeStatus.READY,
        build_log=None,
        base_volume_id=7,
        template_vmstate=None,
        template_memory=None,
        created_at="t",
        built_at="t",
    )


def _receiver() -> tuple[FastAPI, list[dict[str, Any]]]:
    app = FastAPI()
    received: list[dict[str, Any]] = []

    @app.post("/cb")
    async def cb(request: Request) -> dict[str, str]:
        received.append(await request.json())
        return {"ok": "yes"}

    return app, received


async def _lifecycle(
    db: aiosqlite.Connection, tmp_path: Path
) -> tuple[Lifecycle, ComputerService, CheckpointService, FakeHostInstance, list[dict[str, Any]]]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    tasks = BackgroundTasks()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, tasks)
    computers = ComputerService(config, db, host, allocator, recipes)
    checkpoints = CheckpointService(config, db, host, allocator, computers, tasks)
    app, received = _receiver()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://receiver")
    lifecycle = Lifecycle(db, computers, checkpoints, tasks, http)
    return lifecycle, computers, checkpoints, host, received


async def test_no_command_means_no_exec_and_no_self_destruct(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, _, host, _ = await _lifecycle(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    spec = ExecSpec(command=None, self_destruct=True, callback_url=None, label="x", meta_exec=None)
    result = await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    assert result.exec_exit_code is None and result.created_checkpoint_id is None
    assert host.guest.commands == []
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.RUNNING


async def test_self_destruct_checkpoints_destroys_and_calls_back(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, _, host, received = await _lifecycle(db, tmp_path)
    computer = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["echo done"] = ExecResult(0, "done\n", "err\n")
    before = checkpoints_total.labels(trigger="self_destruct")._value.get()
    spec = ExecSpec(
        command="echo done",
        self_destruct=True,
        callback_url="http://receiver/cb",
        label="chain",
        meta_exec=None,
    )
    result = await lifecycle.run_ephemeral(ACCOUNT, computer, spec, source_checkpoint=None)
    assert result.exec_exit_code == 0 and result.created_checkpoint_id is not None
    assert host.guest.commands == [(computer.vm_ip, "echo done"), (computer.vm_ip, "sync")]
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert checkpoints_total.labels(trigger="self_destruct")._value.get() == before + 1
    await lifecycle.tasks.drain(timeout=2.0)
    assert received == [
        {
            "computer_id": computer.id,
            "checkpoint_id": None,
            "label": "chain",
            "exec_exit_code": 0,
            "exec_stdout": "done\n",
            "exec_stderr": "err\n",
            "created_checkpoint_id": result.created_checkpoint_id,
        }
    ]


async def test_fork_self_destruct_inherits_the_source_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, _host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    spec = ExecSpec(
        command="true", self_destruct=True, callback_url=None, label=None, meta_exec=None
    )
    result = await lifecycle.run_ephemeral(ACCOUNT, fork, spec, source_checkpoint=source)
    created = await checkpoints.get_owned(ACCOUNT, result.created_checkpoint_id or "")
    assert created.label == "chain" and created.parent_id == source.id


async def test_drain_forks_once_writes_exec_files_and_self_destructs(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, received = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await computers.destroy(base.id)
    for i, payload in enumerate(
        [
            {"checkpoint_id": source.id, "exec": "echo 'hi'", "self_destruct": False},
            {
                "checkpoint_id": source.id,
                "exec": "echo two",
                "self_destruct": True,
                "callback_url": "http://receiver/cb",
                "meta_exec": "bash /tmp/exec/1.txt",
            },
        ]
    ):
        await insert_deferred(db, f"def-{i}", "chain", "acct-1", json.dumps(payload), f"t{i}")
    await asyncio.gather(
        lifecycle.drain_deferred(ACCOUNT, "chain"), lifecycle.drain_deferred(ACCOUNT, "chain")
    )
    forks = [c for c in host.hypervisor.restored if c[0] != base.thin_volume_id]
    assert len(forks) == 1, "two concurrent drains must fork exactly once"
    commands = [cmd for _, cmd in host.guest.commands if cmd not in ("sync",)]
    # Every claimed exec is written verbatim, with single quotes escaped the shell's way.
    assert commands[-2] == (
        "mkdir -p /tmp/exec"
        " && printf '%s' 'echo '\\''hi'\\''' > /tmp/exec/0.txt"
        " && printf '%s' 'echo two' > /tmp/exec/1.txt"
    )
    assert commands[-1] == "bash /tmp/exec/1.txt"  # last meta_exec wins
    await lifecycle.tasks.drain(timeout=2.0)
    assert len(received) == 1 and received[0]["label"] == "chain"
    assert await claim_deferred_by_label(db, "chain") == []


async def test_drain_with_no_labelled_checkpoint_logs_and_returns(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, _, _, host, _ = await _lifecycle(db, tmp_path)
    await insert_deferred(db, "def-x", "orphan", "acct-1", "{}", "t")
    await lifecycle.drain_deferred(ACCOUNT, "orphan")
    assert host.hypervisor.restored == [] and host.hypervisor.booted == []


async def test_drain_forks_with_the_recipe_id_from_the_payload(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """The deferred payload's recipe_id wins over the checkpoint's own."""
    lifecycle, computers, checkpoints, _host, _ = await _lifecycle(db, tmp_path)
    await insert_recipe(db, _ready_recipe("rec-1"))
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    assert source.recipe_id is None  # so the payload is the only source of the id
    payload = {"checkpoint_id": source.id, "exec": "echo hi", "recipe_id": "rec-1"}
    await insert_deferred(db, "def-0", "chain", "acct-1", json.dumps(payload), "t0")
    await lifecycle.drain_deferred(ACCOUNT, "chain")
    forked = [c for c in await list_all_computers(db) if c.id != base.id]
    assert len(forked) == 1 and forked[0].recipe_id == "rec-1"


async def test_spawn_drain_defers_the_work_to_the_background(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    await insert_deferred(db, "def-0", "chain", "acct-1", json.dumps({"exec": "echo q"}), "t0")
    restored_before = len(host.hypervisor.restored)
    lifecycle.spawn_drain(ACCOUNT, "chain")
    assert len(host.hypervisor.restored) == restored_before  # nothing ran synchronously
    await lifecycle.tasks.drain(timeout=2.0)
    assert len(host.hypervisor.restored) == restored_before + 1
    assert await claim_deferred_by_label(db, "chain") == []


async def test_drain_after_destroy_drains_the_source_label(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    await computers.destroy(fork.id)
    await insert_deferred(db, "def-0", "chain", "acct-1", json.dumps({"exec": "echo q"}), "t0")
    restored_before = len(host.hypervisor.restored)
    await lifecycle.drain_after_destroy(ACCOUNT, fork)
    await lifecycle.tasks.drain(timeout=2.0)
    assert len(host.hypervisor.restored) == restored_before + 1
    assert await claim_deferred_by_label(db, "chain") == []


async def test_drain_after_destroy_tolerates_a_pruned_source_checkpoint(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    """prune() deletes checkpoints without checking for live forks, so a destroy
    must not turn into a NotFound just because the source row is gone."""
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    await computers.destroy(fork.id)
    await insert_deferred(db, "def-0", "chain", "acct-1", json.dumps({"exec": "echo q"}), "t0")
    await delete_checkpoint(db, source.id)
    restored_before = len(host.hypervisor.restored)
    await lifecycle.drain_after_destroy(ACCOUNT, fork)
    await lifecycle.tasks.drain(timeout=2.0)
    assert len(host.hypervisor.restored) == restored_before
    assert [d.id for d in await claim_deferred_by_label(db, "chain")] == ["def-0"]


async def test_drain_after_destroy_ignores_a_source_checkpoint_of_another_account(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    lifecycle, computers, checkpoints, host, _ = await _lifecycle(db, tmp_path)
    await insert_account(db, OTHER)
    base = await computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    source = await checkpoints.create(base, label="chain", trigger=CheckpointTrigger.API)
    fork = await computers.fork(ACCOUNT, source, recipe_id=None)
    await computers.destroy(fork.id)
    await insert_deferred(db, "def-0", "chain", "acct-1", json.dumps({"exec": "echo q"}), "t0")
    await delete_checkpoint(db, source.id)
    await insert_checkpoint(db, replace(source, account_id=OTHER.id))
    restored_before = len(host.hypervisor.restored)
    await lifecycle.drain_after_destroy(ACCOUNT, fork)
    await lifecycle.tasks.drain(timeout=2.0)
    assert len(host.hypervisor.restored) == restored_before
    assert [d.id for d in await claim_deferred_by_label(db, "chain")] == ["def-0"]
