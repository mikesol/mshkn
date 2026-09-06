from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mshkn.config import Config
from mshkn.db import get_computer, insert_account, insert_computer
from mshkn.host.fake import FakeHost
from mshkn.models import Account, Computer, ComputerStatus
from mshkn.runtime import BackgroundTasks
from mshkn.vm.manager import VMManager

if TYPE_CHECKING:
    import aiosqlite

DEAD_PID = 424242


def test_slot_allocation() -> None:
    """Test that slot and volume ID allocation works correctly."""
    # We can't easily test the full create flow without root/Firecracker,
    # but we can test the allocation logic
    config = Config()
    # VMManager needs a db connection, but we just test the allocation methods
    # by accessing the internal state directly
    manager = VMManager.__new__(VMManager)
    manager.config = config
    manager.host = FakeHost()
    manager._next_slot = 1
    manager._free_slots = set()
    manager._next_volume_id = 100
    manager._alloc_lock = asyncio.Lock()
    manager.tasks = BackgroundTasks()

    assert manager._allocate_slot() == 1
    assert manager._allocate_slot() == 2
    assert manager._allocate_volume_id() == 100
    assert manager._allocate_volume_id() == 101

    # Test slot recycling
    manager._release_slot(1)
    assert manager._allocate_slot() == 1  # reuses freed slot
    assert manager._allocate_slot() == 3  # next new slot


async def test_reaper_releases_every_host_resource_of_a_dead_vm(
    db: aiosqlite.Connection,
) -> None:
    """A VM whose Firecracker process died gives back its route, volume, tap, and
    its pooled SSH connection. Leaving the connection behind would hand it to the
    next VM that lands on the recycled slot."""
    await insert_account(
        db,
        Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-03-08T00:00:00"),
    )
    computer = Computer(
        id="comp-dead",
        account_id="acct-1",
        thin_volume_id=107,
        tap_device="tap7",
        vm_ip="172.16.7.2",
        socket_path="/tmp/fc-mshkn-comp-dead.socket",
        firecracker_pid=DEAD_PID,
        manifest_hash="abc",
        manifest_json="{}",
        status=ComputerStatus.RUNNING,
        created_at="2026-03-08T00:00:00",
        last_exec_at=None,
    )
    await insert_computer(db, computer)

    host = FakeHost()
    await host.proxy.add_route("comp-dead", "172.16.7.2")
    await host.guest.warm("172.16.7.2")
    # The fake hypervisor only knows the VMs it booted, so this pid reads as dead.
    assert not host.hypervisor.is_alive(DEAD_PID)

    manager = VMManager(Config(), db, host=host)

    assert await manager.reap_dead_vms() == 1

    assert host.proxy.routes == {}
    assert ("remove", (107, "mshkn-comp-dead")) in host.blocks.calls
    assert host.hypervisor.torn_down == [7]
    assert host.guest.evicted == ["172.16.7.2"]
    assert 7 in manager._free_slots

    stored = await get_computer(db, "comp-dead")
    assert stored is not None
    assert stored.status == ComputerStatus.DESTROYED
