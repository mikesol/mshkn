from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_computer, insert_account, insert_checkpoint
from mshkn.errors import BadRequest, HostError, LimitExceeded, NotFound
from mshkn.host import ExecResult
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Account, Checkpoint, ComputerStatus
from mshkn.observability.metrics import computers_active, operation_errors_total
from mshkn.resources import DEFAULT_RESOURCES, Resources
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.computers import ComputerService
from mshkn.services.recipes import RecipeService

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

ACCOUNT = Account(id="acct-1", api_key="k", vm_limit=2, created_at="t")


async def _clear_last_exec_at(db: aiosqlite.Connection, computer_id: str) -> None:
    await db.execute("UPDATE computers SET last_exec_at = NULL WHERE id = ?", (computer_id,))
    await db.commit()


async def _service(
    db: aiosqlite.Connection, tmp_path: Path
) -> tuple[ComputerService, FakeHostInstance]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, BackgroundTasks())
    return ComputerService(config, db, host, allocator, recipes), host


async def test_create_default_resources_restores_from_the_template(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert computer.status is ComputerStatus.RUNNING and computer.slot == 1
    assert host.blocks.volumes[computer.thin_volume_id] == 0
    assert host.hypervisor.restored[0][0] == computer.thin_volume_id
    assert host.hypervisor.booted == []
    assert host.guest.warmed == [computer.vm_ip]
    assert host.proxy.routes == {computer.id: computer.vm_ip}
    assert await service.active_count_total() == 1
    assert computers_active._value.get() == 1  # gauge set from the DB


async def test_create_custom_resources_cold_boots(db: aiosqlite.Connection, tmp_path: Path) -> None:
    service, host = await _service(db, tmp_path)
    big = Resources(mem_mib=1024, vcpus=4)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=big)
    assert host.hypervisor.booted == [(computer.thin_volume_id, big)]
    assert host.hypervisor.restored == []


async def test_create_enforces_the_vm_limit(db: aiosqlite.Connection, tmp_path: Path) -> None:
    service, _ = await _service(db, tmp_path)
    await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    with pytest.raises(LimitExceeded):
        await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)


async def test_create_unknown_recipe_leaves_no_host_state(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    domain_errors = operation_errors_total.labels(op="create", kind="domain")
    before = domain_errors._value.get()
    with pytest.raises(NotFound):
        await service.create(ACCOUNT, recipe_id="rcp-nope", resources=DEFAULT_RESOURCES)
    assert host.blocks.volumes == {0: None} and service.allocator.free_slots == frozenset()
    # resolve() runs inside timed("create"), so the rejection is counted
    assert domain_errors._value.get() == before + 1


async def test_abandon_survives_a_failing_volume_removal(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, host = await _service(db, tmp_path)

    async def _explode(*, volume_id: int, name: str) -> None:
        raise RuntimeError("dmsetup exploded")

    monkeypatch.setattr(host.blocks, "remove", _explode)
    host.hypervisor.fail_next("boot")
    with pytest.raises(HostError):  # the cleanup failure must not replace it
        await service.create(ACCOUNT, recipe_id=None, resources=Resources(mem_mib=512, vcpus=1))
    assert service.allocator.free_slots == frozenset({1}), "the slot must not be stranded"
    assert host.hypervisor.torn_down == [1], "later steps still run"


async def test_boot_failure_after_snap_releases_volume_and_slot(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    host.hypervisor.fail_next("boot")
    with pytest.raises(HostError):
        await service.create(ACCOUNT, recipe_id=None, resources=Resources(mem_mib=512, vcpus=1))
    assert host.blocks.volumes == {0: None}, "the snapped volume must be removed"
    assert host.hypervisor.torn_down == [1]
    assert service.allocator.free_slots == frozenset({1})
    assert await service.active_count_total() == 0
    # the slot is reusable and the next create works
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert computer.slot == 1


async def test_route_failure_after_boot_kills_the_vm_and_cleans_up(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    host.proxy.fail_next("add_route")
    with pytest.raises(HostError):
        await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert host.hypervisor.alive == {}
    assert host.blocks.volumes == {0: None}
    assert host.proxy.routes == {}
    assert await service.active_count_total() == 0


async def test_destroy_releases_everything_and_is_idempotent(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await service.destroy(computer.id)
    assert host.hypervisor.alive == {} and host.proxy.routes == {}
    assert computer.thin_volume_id not in host.blocks.volumes
    assert host.hypervisor.torn_down == [computer.slot]
    assert host.guest.evicted == [computer.vm_ip]
    assert service.allocator.free_slots == frozenset({computer.slot})
    await service.destroy(computer.id)  # no error, nothing repeated
    assert host.hypervisor.torn_down == [computer.slot]
    with pytest.raises(NotFound):
        await service.destroy("comp-nope")
    assert computers_active._value.get() == 0


async def test_fork_restores_from_checkpoint_files_or_downloads_them(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    await host.blocks.snap(source_volume_id=0, new_volume_id=50)
    ckpt = Checkpoint(
        id="ckpt-1",
        account_id="acct-1",
        parent_id=None,
        computer_id=None,
        thin_volume_id=50,
        r2_prefix="acct-1/ckpt-1",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=None,
        pinned=False,
        created_at="t",
    )
    await insert_checkpoint(db, ckpt)
    host.objects.prefixes["acct-1/ckpt-1"] = {"vmstate": b"v", "memory": b"m"}
    computer = await service.fork(ACCOUNT, ckpt, recipe_id=None)
    assert computer.source_checkpoint_id == "ckpt-1"
    assert host.hypervisor.restored[-1][0] == computer.thin_volume_id
    assert (tmp_path / "ckpts" / "ckpt-1" / "vmstate").read_bytes() == b"v"


async def test_fork_of_a_merge_checkpoint_cold_boots(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    await host.blocks.snap(source_volume_id=0, new_volume_id=50)
    ckpt = Checkpoint(
        id="ckpt-m",
        account_id="acct-1",
        parent_id=None,
        computer_id=None,
        thin_volume_id=50,
        r2_prefix="acct-1/ckpt-m",
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label="merge",
        pinned=False,
        created_at="t",
    )
    await insert_checkpoint(db, ckpt)
    computer = await service.fork(ACCOUNT, ckpt, recipe_id=None)
    assert host.hypervisor.booted == [(computer.thin_volume_id, DEFAULT_RESOURCES)]


async def test_guest_operations_touch_last_exec_at_and_map_errors(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.script["true"] = ExecResult(0, "", "")
    assert (await service.exec(computer, "true")).exit_code == 0
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.last_exec_at is not None
    with pytest.raises(NotFound):
        await service.download(computer, "/nope")
    await service.destroy(computer.id)
    with pytest.raises(BadRequest):
        await service.get_running(ACCOUNT, computer.id)
    with pytest.raises(NotFound):
        await service.get_owned(ACCOUNT, computer.id)
    with pytest.raises(NotFound):
        await service.get_running(
            Account(id="other", api_key="o", vm_limit=1, created_at="t"), computer.id
        )


async def test_cleanup_dead_releases_a_vm_whose_process_is_gone(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert computer.firecracker_pid is not None
    host.hypervisor.alive.pop(computer.firecracker_pid)  # the process died on its own
    await service.cleanup_dead(computer)
    assert host.proxy.routes == {}
    assert computer.thin_volume_id not in host.blocks.volumes
    assert host.hypervisor.torn_down == [computer.slot]
    assert host.guest.evicted == [computer.vm_ip]
    assert service.allocator.free_slots == frozenset({computer.slot})
    stored = await get_computer(db, computer.id)
    assert stored is not None and stored.status is ComputerStatus.DESTROYED
    assert computers_active._value.get() == 0


async def test_streaming_background_and_transfer_operations(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.guest.stream_script["ls"] = [("stdout", "a")]
    await _clear_last_exec_at(db, computer.id)
    assert [line async for line in service.stream(computer, "ls")] == [
        ("stdout", "a"),
        ("exit", "0"),
    ]
    streamed = await get_computer(db, computer.id)
    assert streamed is not None and streamed.last_exec_at is not None, "stream must touch"
    await _clear_last_exec_at(db, computer.id)
    pid = await service.exec_bg(computer, "sleep 1")
    backgrounded = await get_computer(db, computer.id)
    assert backgrounded is not None and backgrounded.last_exec_at is not None, "exec_bg must touch"
    host.guest.script[f"cat /tmp/bg-{pid}.log 2>/dev/null || echo ''"] = ExecResult(0, "a\nb", "")
    assert await service.exec_logs(computer, pid) == ["a", "b"]
    assert (await service.exec_kill(computer, pid)).exit_code == 0
    assert host.guest.commands[-1] == (computer.vm_ip, f"kill {pid}")
    await service.upload(computer, "/tmp/f", b"data")
    assert await service.download(computer, "/tmp/f") == b"data"
    assert await service.metrics(computer) == host.guest.default_metrics
    host.guest.fail_next("metrics")
    assert await service.metrics(computer) is None
