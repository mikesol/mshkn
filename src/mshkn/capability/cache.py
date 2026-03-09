"""Capability volume cache backed by the capability_cache DB table."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def get_cached_volume(db: aiosqlite.Connection, manifest_hash: str) -> int | None:
    """Return the cached volume_id for manifest_hash, or None on miss."""
    cursor = await db.execute(
        "SELECT volume_id FROM capability_cache WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    await db.execute(
        "UPDATE capability_cache SET last_used_at = datetime('now') WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    await db.commit()
    result: int = row[0]
    return result


async def cache_volume(
    db: aiosqlite.Connection,
    manifest_hash: str,
    volume_id: int,
    nix_closure_size: int | None = None,
) -> None:
    """Insert or replace a capability volume in the cache."""
    await db.execute(
        "INSERT OR REPLACE INTO capability_cache "
        "(manifest_hash, volume_id, nix_closure_size_bytes) "
        "VALUES (?, ?, ?)",
        (manifest_hash, volume_id, nix_closure_size),
    )
    await db.commit()


async def get_max_capability_volume_id(db: aiosqlite.Connection) -> int | None:
    """Return the highest volume_id in the capability cache, or None if empty."""
    cursor = await db.execute("SELECT MAX(volume_id) FROM capability_cache")
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else None
