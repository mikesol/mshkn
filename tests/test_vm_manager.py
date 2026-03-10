import asyncio

from mshkn.config import Config
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
