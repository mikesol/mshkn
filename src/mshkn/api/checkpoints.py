from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mshkn.api.auth import require_account

if TYPE_CHECKING:
    from mshkn.models import Account

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])

_require_account = Depends(require_account)


class ForkRequest(BaseModel):
    manifest: dict[str, object] | None = None


@router.post("/{checkpoint_id}/fork")
async def fork_checkpoint(
    checkpoint_id: str,
    body: ForkRequest | None = None,
    account: Account = _require_account,) -> dict[str, object]:
    raise NotImplementedError


class MergeRequest(BaseModel):
    checkpoint_a: str
    checkpoint_b: str


@router.post("/merge")
async def merge_checkpoints(
    body: MergeRequest,
    account: Account = _require_account,) -> dict[str, object]:
    raise NotImplementedError


@router.get("")
async def list_checkpoints(
    account: Account = _require_account,) -> list[dict[str, object]]:
    raise NotImplementedError


@router.delete("/{checkpoint_id}")
async def delete_checkpoint(
    checkpoint_id: str,
    account: Account = _require_account,) -> dict[str, str]:
    raise NotImplementedError
