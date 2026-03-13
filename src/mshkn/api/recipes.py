from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.db import (
    count_recipe_references,
    delete_failed_recipes_by_hash,
    delete_recipe,
    get_recipe,
    get_recipe_by_content_hash,
    insert_recipe,
    list_recipes_by_account,
)
from mshkn.models import Recipe
from mshkn.recipe.builder import build_recipe, dockerfile_content_hash
from mshkn.vm.storage import remove_volume

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account
    from mshkn.vm.manager import VMManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])

_require_account = Depends(require_account)

# Per-account build serialization locks
_build_locks: dict[str, asyncio.Lock] = {}


def _get_build_lock(account_id: str) -> asyncio.Lock:
    """Lazily create and return a per-account asyncio.Lock."""
    if account_id not in _build_locks:
        _build_locks[account_id] = asyncio.Lock()
    return _build_locks[account_id]


class CreateRecipeRequest(BaseModel):
    dockerfile: str


class RecipeResponse(BaseModel):
    recipe_id: str
    status: str
    content_hash: str
    build_log: str | None = None
    base_volume_id: int | None = None
    created_at: str | None = None
    built_at: str | None = None


def _recipe_to_response(recipe: Recipe) -> RecipeResponse:
    return RecipeResponse(
        recipe_id=recipe.id,
        status=recipe.status,
        content_hash=recipe.content_hash,
        build_log=recipe.build_log,
        base_volume_id=recipe.base_volume_id,
        created_at=recipe.created_at,
        built_at=recipe.built_at,
    )


@router.post("")
async def create_recipe(
    request: Request,
    body: CreateRecipeRequest,
    account: Account = _require_account,
) -> JSONResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    vm_mgr: VMManager = request.app.state.vm_manager

    content_hash = dockerfile_content_hash(body.dockerfile)

    # Return existing non-failed recipe if it exists
    existing = await get_recipe_by_content_hash(db, account.id, content_hash)
    if existing is not None:
        return JSONResponse(
            status_code=200,
            content=_recipe_to_response(existing).model_dump(),
        )

    # Clean up any prior failed attempts with this hash
    await delete_failed_recipes_by_hash(db, account.id, content_hash)

    # Create new pending recipe
    recipe_id = f"rcp-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC).isoformat()
    recipe = Recipe(
        id=recipe_id,
        account_id=account.id,
        dockerfile=body.dockerfile,
        content_hash=content_hash,
        status="pending",
        build_log=None,
        base_volume_id=None,
        template_vmstate=None,
        template_memory=None,
        created_at=now,
        built_at=None,
    )
    await insert_recipe(db, recipe)

    # Allocate a volume ID under the alloc lock
    async with vm_mgr._alloc_lock:
        volume_id = vm_mgr._allocate_volume_id()

    # Start background build task, serialized per account
    build_lock = _get_build_lock(account.id)

    async def _run_build() -> None:
        async with build_lock:
            await build_recipe(db, config, recipe_id, body.dockerfile, content_hash, volume_id)

    task = asyncio.create_task(_run_build())
    vm_mgr._bg_tasks.add(task)
    task.add_done_callback(vm_mgr._bg_tasks.discard)

    return JSONResponse(
        status_code=202,
        content=_recipe_to_response(recipe).model_dump(),
    )


@router.get("/{recipe_id}")
async def get_recipe_endpoint(
    recipe_id: str,
    request: Request,
    account: Account = _require_account,
) -> RecipeResponse:
    db: aiosqlite.Connection = request.app.state.db
    recipe = await get_recipe(db, recipe_id)
    if recipe is None or recipe.account_id != account.id:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return _recipe_to_response(recipe)


@router.get("")
async def list_recipes(
    request: Request,
    account: Account = _require_account,
) -> list[RecipeResponse]:
    db: aiosqlite.Connection = request.app.state.db
    recipes = await list_recipes_by_account(db, account.id)
    return [_recipe_to_response(r) for r in recipes]


@router.delete("/{recipe_id}")
async def delete_recipe_endpoint(
    recipe_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, str]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    recipe = await get_recipe(db, recipe_id)
    if recipe is None or recipe.account_id != account.id:
        raise HTTPException(status_code=404, detail="Recipe not found")

    ref_count = await count_recipe_references(db, recipe_id)
    if ref_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Recipe is referenced by {ref_count} computer(s)/checkpoint(s)",
        )

    if recipe.base_volume_id is not None:
        volume_name = f"mshkn-recipe-{recipe.content_hash[:16]}"
        try:
            await remove_volume(config.thin_pool_name, volume_name, recipe.base_volume_id)
        except Exception:
            logger.exception(
                "Failed to remove volume %s (vol %d) for recipe %s",
                volume_name, recipe.base_volume_id, recipe_id,
            )

    await delete_recipe(db, recipe_id)
    return {"status": "deleted"}
