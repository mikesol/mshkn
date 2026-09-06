from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mshkn.db import claim_deferred_by_label, insert_deferred

if TYPE_CHECKING:
    import aiosqlite


async def _queue(db: aiosqlite.Connection, label: str, n: int) -> None:
    for i in range(n):
        await insert_deferred(
            db, f"def-{label}-{i}", label, "acct-1", "{}", f"2026-09-06T00:00:0{i}"
        )


async def test_claim_returns_items_oldest_first_and_empties_the_label(
    db: aiosqlite.Connection,
) -> None:
    await _queue(db, "chain", 3)
    await _queue(db, "other", 1)
    items = await claim_deferred_by_label(db, "chain")
    assert [i.id for i in items] == ["def-chain-0", "def-chain-1", "def-chain-2"]
    assert await claim_deferred_by_label(db, "chain") == []
    assert [i.id for i in await claim_deferred_by_label(db, "other")] == ["def-other-0"]


async def test_concurrent_claims_hand_out_each_item_exactly_once(
    db: aiosqlite.Connection,
) -> None:
    await _queue(db, "chain", 5)
    a, b = await asyncio.gather(
        claim_deferred_by_label(db, "chain"), claim_deferred_by_label(db, "chain")
    )
    ids = sorted(i.id for i in a + b)
    assert ids == [f"def-chain-{i}" for i in range(5)]
    assert not (a and b), "one claimer must get everything, the other nothing"
