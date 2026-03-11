import asyncio

import pytest

from mshkn.config import Config
from mshkn.vm import manager as vm_manager_module
from mshkn.vm.manager import VMManager


def test_slot_allocation() -> None:
    """Test that slot and volume ID allocation works correctly."""
    # We can't easily test the full create flow without root/Firecracker,
    # but we can test the allocation logic
    config = Config()
    # VMManager needs a db connection, but we just test the allocation methods
    # by accessing the internal state directly
    manager = VMManager.__new__(VMManager)
    manager.config = config
    manager._next_slot = 1
    manager._free_slots = []
    manager._next_volume_id = 100
    manager._alloc_lock = asyncio.Lock()

    assert manager._allocate_slot() == 1
    assert manager._allocate_slot() == 2
    assert manager._allocate_volume_id() == 100
    assert manager._allocate_volume_id() == 101

    # Test slot recycling
    manager._release_slot(1)
    assert manager._allocate_slot() == 1  # reuses freed slot
    assert manager._allocate_slot() == 3  # next new slot


@pytest.mark.asyncio
async def test_start_firecracker_with_snapshot_overlaps_snapshot_and_process_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = VMManager.__new__(VMManager)
    manager.config = Config()

    order: list[str] = []
    release_snapshot = asyncio.Event()

    async def fake_create_snapshot(
        pool_name: str,
        source_volume_id: int,
        new_volume_id: int,
        new_volume_name: str,
        sectors: int,
    ) -> None:
        _ = (pool_name, source_volume_id, new_volume_id, new_volume_name, sectors)
        order.append("snapshot-start")
        await release_snapshot.wait()
        order.append("snapshot-done")

    async def fake_start_firecracker_process(socket_path: str) -> int:
        _ = socket_path
        order.append("firecracker-start")
        return 123

    async def fake_kill_firecracker_process(pid: int) -> None:
        order.append(f"kill-{pid}")

    monkeypatch.setattr(vm_manager_module, "create_snapshot", fake_create_snapshot)
    monkeypatch.setattr(
        vm_manager_module,
        "start_firecracker_process",
        fake_start_firecracker_process,
    )
    monkeypatch.setattr(
        vm_manager_module,
        "kill_firecracker_process",
        fake_kill_firecracker_process,
    )

    task = asyncio.create_task(
        manager._start_firecracker_with_snapshot(
            source_volume_id=10,
            volume_id=20,
            volume_name="mshkn-comp-test",
            socket_path="/tmp/fc-test.socket",
        )
    )

    await asyncio.sleep(0)
    assert "firecracker-start" in order
    assert "snapshot-done" not in order

    release_snapshot.set()
    pid = await task
    assert pid == 123
    assert "snapshot-start" in order
    assert order.index("firecracker-start") < order.index("snapshot-done")
