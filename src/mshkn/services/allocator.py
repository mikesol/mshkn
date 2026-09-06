"""Slot and volume-id allocation (spec §6.1).

State is derived at startup from the database (running computers, highest
checkpoint and recipe volume) and from the pool itself, so orphaned volumes
the database never heard of cannot be reused.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from mshkn.db import (
    get_max_checkpoint_volume_id,
    get_max_recipe_volume_id,
    list_all_computers,
)
from mshkn.errors import LimitExceeded
from mshkn.host.firecracker import STAGING_SLOT
from mshkn.models import ComputerStatus

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.host import BlockStore

logger = logging.getLogger(__name__)

_FIRST_VOLUME_ID = 100  # volume 0 is the base image; leave room below
_LAST_SLOT = 255


class SlotAllocator:
    def __init__(self) -> None:
        self._next_slot = 1
        self._free_slots: set[int] = set()
        self._next_volume_id = _FIRST_VOLUME_ID
        self._lock = asyncio.Lock()

    @property
    def next_slot(self) -> int:
        return self._next_slot

    @property
    def next_volume_id(self) -> int:
        return self._next_volume_id

    @property
    def free_slots(self) -> frozenset[int]:
        return frozenset(self._free_slots)

    async def initialize(self, db: aiosqlite.Connection, blocks: BlockStore) -> None:
        computers = await list_all_computers(db)
        max_vol = _FIRST_VOLUME_ID - 1
        if computers:
            max_vol = max(max_vol, max(c.thin_volume_id for c in computers))
        running = [c for c in computers if c.status == ComputerStatus.RUNNING]
        if running:
            active = {c.slot for c in running}
            self._next_slot = min(max(active) + 1, _LAST_SLOT + 1)
            self._free_slots = {s for s in range(1, self._next_slot) if s not in active}
        else:
            self._next_slot = 1
            self._free_slots = set()
        self._free_slots.discard(STAGING_SLOT)
        for candidate in (
            await get_max_checkpoint_volume_id(db),
            await get_max_recipe_volume_id(db),
            await blocks.max_volume_id(),
        ):
            if candidate is not None:
                max_vol = max(max_vol, candidate)
        self._next_volume_id = max_vol + 1
        logger.info(
            "allocator initialized: next_slot=%d free=%d next_volume_id=%d",
            self._next_slot,
            len(self._free_slots),
            self._next_volume_id,
        )

    async def acquire(self) -> tuple[int, int]:
        async with self._lock:
            return self._take_slot(), self._take_volume_id()

    async def acquire_volume_id(self) -> int:
        async with self._lock:
            return self._take_volume_id()

    async def release_slot(self, slot: int) -> None:
        async with self._lock:
            if slot != STAGING_SLOT:
                self._free_slots.add(slot)

    def _take_slot(self) -> int:
        self._free_slots.discard(STAGING_SLOT)
        if self._free_slots:
            return self._free_slots.pop()
        slot = self._next_slot
        if slot == STAGING_SLOT:
            slot = STAGING_SLOT + 1
        if slot > _LAST_SLOT:
            raise LimitExceeded("No free VM slots")
        self._next_slot = slot + 1
        return slot

    def _take_volume_id(self) -> int:
        volume_id = self._next_volume_id
        self._next_volume_id += 1
        return volume_id
