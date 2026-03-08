"""Capability image cache backed by the capability_cache DB table."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def get_cached_image(db: aiosqlite.Connection, manifest_hash: str) -> Path | None:
    """Return the cached image path for *manifest_hash*, or ``None`` on miss.

    Updates ``last_used_at`` on cache hit.
    """
    cursor = await db.execute(
        "SELECT image_path FROM capability_cache WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    # Touch last_used_at
    await db.execute(
        "UPDATE capability_cache SET last_used_at = datetime('now') WHERE manifest_hash = ?",
        (manifest_hash,),
    )
    await db.commit()

    return Path(row[0])


async def cache_image(
    db: aiosqlite.Connection,
    manifest_hash: str,
    image_path: Path,
    nix_size: int,
    image_size: int,
) -> None:
    """Insert or replace a capability image in the cache."""
    await db.execute(
        "INSERT OR REPLACE INTO capability_cache "
        "(manifest_hash, image_path, nix_closure_size_bytes, image_size_bytes) "
        "VALUES (?, ?, ?, ?)",
        (manifest_hash, str(image_path), nix_size, image_size),
    )
    await db.commit()
