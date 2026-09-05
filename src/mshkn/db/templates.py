"""snapshot_templates table."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def get_bare_template(
    db: aiosqlite.Connection,
) -> tuple[str, str] | None:
    """Get cached bare (no-recipe) L3 template paths."""
    cursor = await db.execute(
        "SELECT vmstate_path, memory_path FROM snapshot_templates WHERE manifest_hash = 'bare'"
    )
    row = await cursor.fetchone()
    if row:
        return (row[0], row[1])
    return None


async def cache_bare_template(
    db: aiosqlite.Connection,
    vmstate_path: str,
    memory_path: str,
) -> None:
    """Cache the bare (no-recipe) L3 template."""
    await db.execute(
        "INSERT OR REPLACE INTO snapshot_templates (manifest_hash, vmstate_path, memory_path) "
        "VALUES ('bare', ?, ?)",
        (vmstate_path, memory_path),
    )
    await db.commit()
