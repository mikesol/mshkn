"""recipes table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.models import Recipe, RecipeStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

COLUMNS: tuple[str, ...] = (
    "id",
    "account_id",
    "dockerfile",
    "content_hash",
    "status",
    "build_log",
    "base_volume_id",
    "template_vmstate",
    "template_memory",
    "created_at",
    "built_at",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM recipes"


def _row_to_recipe(row: Sequence[object]) -> Recipe:
    d = dict(zip(COLUMNS, row, strict=True))
    return Recipe(
        id=str(d["id"]),
        account_id=str(d["account_id"]),
        dockerfile=str(d["dockerfile"]),
        content_hash=str(d["content_hash"]),
        status=RecipeStatus(str(d["status"])),
        build_log=None if d["build_log"] is None else str(d["build_log"]),
        base_volume_id=None if d["base_volume_id"] is None else int(d["base_volume_id"]),  # type: ignore[call-overload]
        template_vmstate=None if d["template_vmstate"] is None else str(d["template_vmstate"]),
        template_memory=None if d["template_memory"] is None else str(d["template_memory"]),
        created_at=str(d["created_at"]),
        built_at=None if d["built_at"] is None else str(d["built_at"]),
    )


async def insert_recipe(db: aiosqlite.Connection, recipe: Recipe) -> None:
    await db.execute(
        "INSERT INTO recipes (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        (
            recipe.id,
            recipe.account_id,
            recipe.dockerfile,
            recipe.content_hash,
            recipe.status,
            recipe.build_log,
            recipe.base_volume_id,
            recipe.template_vmstate,
            recipe.template_memory,
            recipe.created_at,
            recipe.built_at,
        ),
    )
    await db.commit()


async def get_recipe(db: aiosqlite.Connection, recipe_id: str) -> Recipe | None:
    cursor = await db.execute(_SELECT + " WHERE id = ?", (recipe_id,))
    row = await cursor.fetchone()
    return None if row is None else _row_to_recipe(row)


async def get_recipe_by_content_hash(
    db: aiosqlite.Connection, account_id: str, content_hash: str
) -> Recipe | None:
    """Find a non-failed recipe by account and content hash."""
    cursor = await db.execute(
        _SELECT + " WHERE account_id = ? AND content_hash = ? AND status != 'failed' LIMIT 1",
        (account_id, content_hash),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_recipe(row)


async def list_recipes_by_account(db: aiosqlite.Connection, account_id: str) -> list[Recipe]:
    cursor = await db.execute(
        _SELECT + " WHERE account_id = ? ORDER BY created_at DESC",
        (account_id,),
    )
    return [_row_to_recipe(r) for r in await cursor.fetchall()]


async def update_recipe_status(
    db: aiosqlite.Connection, recipe_id: str, status: RecipeStatus
) -> None:
    await db.execute("UPDATE recipes SET status = ? WHERE id = ?", (status, recipe_id))
    await db.commit()


async def update_recipe_build_result(
    db: aiosqlite.Connection,
    recipe_id: str,
    *,
    status: RecipeStatus,
    build_log: str | None = None,
    base_volume_id: int | None = None,
    built_at: str | None = None,
) -> None:
    await db.execute(
        "UPDATE recipes SET status = ?, build_log = ?, base_volume_id = ?, built_at = ? "
        "WHERE id = ?",
        (status, build_log, base_volume_id, built_at, recipe_id),
    )
    await db.commit()


async def update_recipe_template(
    db: aiosqlite.Connection,
    recipe_id: str,
    template_vmstate: str,
    template_memory: str,
) -> None:
    await db.execute(
        "UPDATE recipes SET template_vmstate = ?, template_memory = ? WHERE id = ?",
        (template_vmstate, template_memory, recipe_id),
    )
    await db.commit()


async def delete_recipe(db: aiosqlite.Connection, recipe_id: str) -> None:
    await db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    await db.commit()


async def delete_failed_recipes_by_hash(
    db: aiosqlite.Connection, account_id: str, content_hash: str
) -> None:
    """Delete all failed recipes for the given account and content hash."""
    await db.execute(
        "DELETE FROM recipes WHERE account_id = ? AND content_hash = ? AND status = 'failed'",
        (account_id, content_hash),
    )
    await db.commit()


async def get_max_recipe_volume_id(db: aiosqlite.Connection) -> int | None:
    """Return the highest base_volume_id across all recipes, or None."""
    cursor = await db.execute(
        "SELECT MAX(base_volume_id) FROM recipes WHERE base_volume_id IS NOT NULL"
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else None


async def count_recipe_references(db: aiosqlite.Connection, recipe_id: str) -> int:
    """Count non-destroyed computers + all checkpoints referencing this recipe."""
    cursor = await db.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM computers WHERE recipe_id = ? AND status != 'destroyed') + "
        "(SELECT COUNT(*) FROM checkpoints WHERE recipe_id = ?)",
        (recipe_id, recipe_id),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0
