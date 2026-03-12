"""L3 capability memory cache — FC memory snapshots per manifest hash.

Stores vmstate + memory files so that create() can use LOAD_SNAPSHOT
instead of cold boot when a capability volume already has a cached template.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def get_cached_template(
    db: aiosqlite.Connection, manifest_hash: str
) -> tuple[str, str] | None:
    """Return (vmstate_path, memory_path) for manifest_hash, or None on miss."""
    cursor = await db.execute(
        "SELECT vmstate_path, memory_path FROM snapshot_templates WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    vmstate_path, memory_path = row[0], row[1]
    # Verify files still exist
    if not Path(vmstate_path).exists() or not Path(memory_path).exists():
        logger.warning("L3 cache hit but files missing for %s, evicting", manifest_hash)
        await db.execute(
            "DELETE FROM snapshot_templates WHERE manifest_hash = ?",
            (manifest_hash,),
        )
        await db.commit()
        return None
    return vmstate_path, memory_path


async def cache_template(
    db: aiosqlite.Connection,
    manifest_hash: str,
    vmstate_path: str,
    memory_path: str,
) -> None:
    """Store a template snapshot in the L3 cache."""
    await db.execute(
        "INSERT OR REPLACE INTO snapshot_templates "
        "(manifest_hash, vmstate_path, memory_path) VALUES (?, ?, ?)",
        (manifest_hash, vmstate_path, memory_path),
    )
    await db.commit()
    logger.info("Cached L3 template for %s", manifest_hash)


async def evict_template(db: aiosqlite.Connection, manifest_hash: str) -> None:
    """Remove a template from the L3 cache and delete its files."""
    cursor = await db.execute(
        "SELECT vmstate_path, memory_path FROM snapshot_templates WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    row = await cursor.fetchone()
    if row is not None:
        for path in (row[0], row[1]):
            p = Path(path)
            if p.exists():
                p.unlink()
        # Also remove the parent directory if empty
        parent = Path(row[0]).parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    await db.execute(
        "DELETE FROM snapshot_templates WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    await db.commit()
    logger.info("Evicted L3 template for %s", manifest_hash)
