from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mshkn.db import insert_account, insert_checkpoint, insert_computer, insert_recipe
from mshkn.errors import LimitExceeded
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Checkpoint, Computer, ComputerStatus, Recipe, RecipeStatus
from mshkn.services.allocator import SlotAllocator

if TYPE_CHECKING:
    import aiosqlite


async def test_fresh_allocator_starts_at_slot_1_and_volume_100() -> None:
    alloc = SlotAllocator()
    assert await alloc.acquire() == (1, 100)
    assert await alloc.acquire() == (2, 101)
    assert await alloc.acquire_volume_id() == 102


async def test_released_slots_are_reused_before_new_ones() -> None:
    alloc = SlotAllocator()
    await alloc.acquire()
    await alloc.acquire()
    await alloc.release_slot(1)
    assert (await alloc.acquire())[0] == 1
    assert (await alloc.acquire())[0] == 3


async def test_staging_slot_is_skipped_and_never_recycled() -> None:
    alloc = SlotAllocator()
    for _ in range(253):
        await alloc.acquire()
    assert (await alloc.acquire())[0] == 255
    await alloc.release_slot(254)  # a bug elsewhere must not put 254 in circulation
    with pytest.raises(LimitExceeded):
        await alloc.acquire()


async def test_initialize_derives_state_from_db_and_pool(db: aiosqlite.Connection) -> None:
    await insert_account(db, Account(id="acct-1", api_key="k", vm_limit=10, created_at="t"))
    await insert_computer(
        db,
        Computer(
            id="comp-a",
            account_id="acct-1",
            thin_volume_id=120,
            tap_device="tap3",
            vm_ip="172.16.3.2",
            socket_path="/tmp/fc-mshkn-comp-a.socket",
            firecracker_pid=1,
            status=ComputerStatus.RUNNING,
            created_at="t",
            last_exec_at=None,
        ),
    )
    await insert_checkpoint(
        db,
        Checkpoint(
            id="ckpt-a",
            account_id="acct-1",
            parent_id=None,
            computer_id="comp-a",
            thin_volume_id=150,
            r2_prefix="acct-1/ckpt-a",
            disk_delta_size_bytes=None,
            memory_size_bytes=None,
            label=None,
            pinned=False,
            created_at="t",
        ),
    )
    await insert_recipe(
        db,
        Recipe(
            id="rcp-a",
            account_id="acct-1",
            dockerfile="FROM x",
            content_hash="h",
            status=RecipeStatus.READY,
            build_log=None,
            base_volume_id=160,
            template_vmstate=None,
            template_memory=None,
            created_at="t",
            built_at="t",
        ),
    )
    host = FakeHost()
    host.blocks.volumes[170] = 0  # an orphan the DB does not know about
    alloc = SlotAllocator()
    await alloc.initialize(db, host.blocks)
    assert alloc.next_volume_id == 171
    assert alloc.free_slots == frozenset({1, 2})  # gaps below the highest running slot
    assert (await alloc.acquire())[0] in {1, 2}
    await alloc.release_slot(1)
    await alloc.release_slot(2)
    await alloc.acquire()
    await alloc.acquire()
    assert (await alloc.acquire())[0] == 4  # next after tap3
