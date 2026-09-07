"""The base-volume pipeline: build mshkn-base, export it, write volume 0, drop the bare template."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.db import cache_bare_template, clear_bare_template, get_bare_template

if TYPE_CHECKING:
    import aiosqlite


async def test_clear_bare_template_removes_the_row_and_tolerates_none(
    db: aiosqlite.Connection,
) -> None:
    await cache_bare_template(db, "/t/vmstate", "/t/memory")
    assert await get_bare_template(db) == ("/t/vmstate", "/t/memory")
    await clear_bare_template(db)
    assert await get_bare_template(db) is None
    await clear_bare_template(db)  # nothing to delete is not an error
    assert await get_bare_template(db) is None
