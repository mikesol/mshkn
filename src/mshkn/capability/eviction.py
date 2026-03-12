"""LRU eviction for capability base volumes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mshkn.vm.storage import remove_volume

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def evict_lru_capabilities(
    db: aiosqlite.Connection,
    pool_name: str,
    min_free_gb: float = 5.0,
) -> int:
    """Evict least-recently-used capability volumes until free space > min_free_gb.

    Returns the number of volumes evicted.
    """
    from mshkn.shell import run

    evicted = 0

    while True:
        # Check free space
        try:
            output = await run("df -BG /opt/mshkn/ | tail -1 | awk '{print $4}'")
            free_gb = float(output.strip().rstrip("G"))
        except Exception:
            break

        if free_gb >= min_free_gb:
            break

        # Find LRU entry
        cursor = await db.execute(
            "SELECT manifest_hash, volume_id FROM capability_cache "
            "ORDER BY last_used_at ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            break

        manifest_hash, volume_id = row[0], row[1]
        volume_name = f"mshkn-cap-{manifest_hash}"

        logger.info("Evicting capability volume %s (vol %d)", manifest_hash, volume_id)

        try:
            await remove_volume(pool_name, volume_name, volume_id)
        except Exception as e:
            logger.warning("Failed to remove volume %s: %s", volume_name, e)

        await db.execute(
            "DELETE FROM capability_cache WHERE manifest_hash = ?",
            (manifest_hash,),
        )
        await db.commit()

        # Also evict L3 template if present
        from mshkn.capability.template_cache import evict_template
        await evict_template(db, manifest_hash)

        evicted += 1

    return evicted
