"""Recipe endpoints. The build pipeline lives in mshkn.services.recipes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mshkn.api import scopes
from mshkn.api.deps import get_runtime, require_principal
from mshkn.api.schemas import CreateRecipeRequest, DeleteResponse, RecipeResponse

if TYPE_CHECKING:
    from mshkn.models import Principal, Recipe

router = APIRouter(prefix="/recipes", tags=["recipes"])

_require_principal = Depends(require_principal)


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


@router.post(
    "",
    response_model=RecipeResponse,
    status_code=202,
    responses={200: {"model": RecipeResponse}},
)
async def create_recipe(
    request: Request,
    body: CreateRecipeRequest,
    principal: Principal = _require_principal,
) -> JSONResponse:
    account = principal.account
    scopes.require_recipes_create(principal)
    rt = get_runtime(request)
    recipe, created = await rt.recipes.create(account, body.dockerfile)
    return JSONResponse(
        status_code=202 if created else 200,
        content=_recipe_to_response(recipe).model_dump(),
    )


@router.get("/{recipe_id}", response_model=RecipeResponse)
async def get_recipe_endpoint(
    recipe_id: str,
    request: Request,
    principal: Principal = _require_principal,
) -> RecipeResponse:
    account = principal.account
    scopes.require_recipes_read(principal)
    rt = get_runtime(request)
    return _recipe_to_response(await rt.recipes.get(account, recipe_id))


@router.get("", response_model=list[RecipeResponse])
async def list_recipes(
    request: Request,
    principal: Principal = _require_principal,
) -> list[RecipeResponse]:
    account = principal.account
    scopes.require_recipes_read(principal)
    rt = get_runtime(request)
    return [_recipe_to_response(r) for r in await rt.recipes.list(account)]


@router.delete("/{recipe_id}", response_model=DeleteResponse)
async def delete_recipe_endpoint(
    recipe_id: str,
    request: Request,
    principal: Principal = _require_principal,
) -> DeleteResponse:
    account = principal.account
    scopes.require_account_key(principal)
    rt = get_runtime(request)
    await rt.recipes.delete(account, recipe_id)
    return DeleteResponse(status="deleted")
