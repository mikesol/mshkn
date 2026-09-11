"""One computer, one teardown: destroy and the dead-VM reaper claim it atomically (#70)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mshkn.config import Config
from mshkn.db import get_computer, insert_account
from mshkn.errors import HostError
from mshkn.host.fake import FakeHost, FakeHostInstance
from mshkn.models import Computer, ComputerStatus
from mshkn.resources import DEFAULT_RESOURCES
from mshkn.runtime import BackgroundTasks
from mshkn.services.allocator import SlotAllocator
from mshkn.services.computers import ComputerService
from mshkn.services.recipes import RecipeService
from tests.support import account_row

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import aiosqlite

ACCOUNT = account_row(api_key="k", vm_limit=4)


async def _service(
    db: aiosqlite.Connection, tmp_path: Path
) -> tuple[ComputerService, FakeHostInstance]:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    config = Config(domain="test.dev", checkpoint_local_dir=tmp_path / "ckpts")
    allocator = SlotAllocator()
    recipes = RecipeService(config, db, host.blocks, host.hypervisor, allocator, BackgroundTasks())
    return ComputerService(config, db, host, allocator, recipes), host


class _Gate:
    """Parks one host call so a second teardown can be started while the first is mid-flight."""

    def __init__(self) -> None:
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    def wrap(self, real: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
        async def call(*args: object, **kwargs: object) -> object:
            self.parked.set()
            await self.release.wait()
            return await real(*args, **kwargs)

        return call


async def _status(db: aiosqlite.Connection, computer_id: str) -> ComputerStatus:
    stored = await get_computer(db, computer_id)
    assert stored is not None
    return stored.status


async def test_destroy_and_cleanup_dead_tear_a_computer_down_once(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal case: the reaper's snapshot predates destroy, and destroy is mid-flight."""
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    snapshot: Computer = computer  # what the reaper's _running() saw: status running
    gate = _Gate()
    monkeypatch.setattr(host.hypervisor, "kill", gate.wrap(host.hypervisor.kill))

    destroy = asyncio.create_task(service.destroy(computer.id))
    await gate.parked.wait()
    assert await _status(db, computer.id) is ComputerStatus.DESTROYING

    assert await service.cleanup_dead(snapshot) is False  # lost the claim, touched nothing
    assert host.hypervisor.torn_down == [] and service.allocator.free_slots == frozenset()

    gate.release.set()
    await destroy
    assert host.hypervisor.killed == [computer.firecracker_pid]
    assert host.hypervisor.torn_down == [computer.slot]
    assert host.guest.evicted == [computer.vm_ip]
    assert service.allocator.free_slots == frozenset({computer.slot})
    assert await _status(db, computer.id) is ComputerStatus.DESTROYED


async def test_a_second_destroy_returns_while_the_first_is_still_running(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    gate = _Gate()
    monkeypatch.setattr(host.blocks, "remove", gate.wrap(host.blocks.remove))

    first = asyncio.create_task(service.destroy(computer.id))
    await gate.parked.wait()
    await service.destroy(computer.id)  # returns at once: the teardown is someone else's
    assert not first.done()
    # The tap goes alongside the parked volume removal (#148); the slot waits for both.
    assert service.allocator.free_slots == frozenset()

    gate.release.set()
    await first
    assert host.hypervisor.torn_down == [computer.slot]
    assert service.allocator.free_slots == frozenset({computer.slot})


async def test_cleanup_dead_with_a_stale_snapshot_of_a_destroyed_computer_does_nothing(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await service.destroy(computer.id)
    replacement = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    assert replacement.slot == computer.slot  # the slot was recycled

    assert await service.cleanup_dead(computer) is False

    assert host.hypervisor.torn_down == [computer.slot], "the replacement's tap is intact"
    assert replacement.firecracker_pid in host.hypervisor.alive
    assert service.allocator.free_slots == frozenset()


async def test_cleanup_dead_tears_down_a_dead_vm_it_claims(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    host.hypervisor.alive.pop(computer.firecracker_pid or -1)

    assert await service.cleanup_dead(computer) is True

    assert host.hypervisor.torn_down == [computer.slot]
    assert service.allocator.free_slots == frozenset({computer.slot})
    assert await _status(db, computer.id) is ComputerStatus.DESTROYED


async def test_a_failing_step_does_not_stop_destroy_from_finishing(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once claimed, nobody else will tear it down, so the pass has to run to the end."""
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    async def boom(*, volume_id: int, name: str) -> None:
        raise RuntimeError("dmsetup: device busy")

    monkeypatch.setattr(host.blocks, "remove", boom)

    with pytest.raises(HostError, match=r"volume removal.*device busy"):
        await service.destroy(computer.id)

    assert host.hypervisor.torn_down == [computer.slot]
    assert host.guest.evicted == [computer.vm_ip]
    assert service.allocator.free_slots == frozenset({computer.slot})
    assert await _status(db, computer.id) is ComputerStatus.DESTROYED
    await service.destroy(computer.id)  # already destroyed: a no-op, not a second pass
    assert host.hypervisor.torn_down == [computer.slot]


async def test_claim_teardown_succeeds_exactly_once(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    from mshkn.db import claim_teardown

    service, _ = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    assert await claim_teardown(db, computer.id) is True
    assert await _status(db, computer.id) is ComputerStatus.DESTROYING
    assert await claim_teardown(db, computer.id) is False  # already claimed
    assert await claim_teardown(db, "comp-nope") is False  # no such row


async def test_a_computer_being_torn_down_is_visible_but_not_usable(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    from mshkn.db import claim_teardown
    from mshkn.errors import BadRequest

    service, _ = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    await claim_teardown(db, computer.id)

    owned = await service.get_owned(ACCOUNT, computer.id)
    assert owned.status is ComputerStatus.DESTROYING
    with pytest.raises(BadRequest, match="destroying"):
        await service.get_running(ACCOUNT, computer.id)
    assert await service.active_count(ACCOUNT.id) == 1, "its slot and volume are still held"


async def test_destroy_cancelled_mid_pass_finishes_the_pass_then_propagates(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed teardown cannot be abandoned half-way: nobody else will pick it up."""
    service, host = await _service(db, tmp_path)
    computer = await service.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)
    gate = _Gate()
    monkeypatch.setattr(host.hypervisor, "kill", gate.wrap(host.hypervisor.kill))

    task = asyncio.create_task(service.destroy(computer.id))
    await gate.parked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert computer.thin_volume_id not in host.blocks.volumes, "the pass continued past the cancel"
    assert host.hypervisor.torn_down == [computer.slot]
    assert service.allocator.free_slots == frozenset({computer.slot})
    assert await _status(db, computer.id) is ComputerStatus.DESTROYED
