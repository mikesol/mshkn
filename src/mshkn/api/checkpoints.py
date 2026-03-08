from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.db import delete_checkpoint as db_delete_checkpoint
from mshkn.db import get_checkpoint, list_checkpoints_by_account

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.models import Account

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])

_require_account = Depends(require_account)


class ForkRequest(BaseModel):
    manifest: dict[str, object] | None = None


class ForkResponse(BaseModel):
    computer_id: str
    checkpoint_id: str


@router.post("/{checkpoint_id}/fork", response_model=ForkResponse)
async def fork_checkpoint(
    checkpoint_id: str,
    request: Request,
    body: ForkRequest | None = None,  # noqa: ARG001
    account: Account = _require_account,
) -> ForkResponse:
    db: aiosqlite.Connection = request.app.state.db

    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    # TODO: Full restore path:
    # 1. Download checkpoint from R2 if not cached locally
    # 2. Create new dm-thin snapshot from the checkpoint's volume
    # 3. Start new Firecracker process
    # 4. Load VM snapshot (memory + vmstate)
    # 5. Resume VM
    # For now, raise NotImplementedError with context
    raise NotImplementedError(
        f"Fork from checkpoint {checkpoint_id} not yet implemented — "
        "requires VM restore from snapshot"
    )


class MergeRequest(BaseModel):
    checkpoint_a: str
    checkpoint_b: str


@router.post("/merge")
async def merge_checkpoints(
    body: MergeRequest,
    account: Account = _require_account,
) -> dict[str, object]:
    raise NotImplementedError


@router.get("")
async def list_checkpoints(
    request: Request,
    account: Account = _require_account,
) -> list[dict[str, object]]:
    db: aiosqlite.Connection = request.app.state.db
    checkpoints = await list_checkpoints_by_account(db, account.id)
    return [
        {
            "id": c.id,
            "parent_id": c.parent_id,
            "computer_id": c.computer_id,
            "manifest_hash": c.manifest_hash,
            "r2_prefix": c.r2_prefix,
            "disk_delta_size_bytes": c.disk_delta_size_bytes,
            "memory_size_bytes": c.memory_size_bytes,
            "label": c.label,
            "pinned": c.pinned,
            "created_at": c.created_at,
        }
        for c in checkpoints
    ]


@router.delete("/{checkpoint_id}")
async def delete_checkpoint(
    checkpoint_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, str]:
    db: aiosqlite.Connection = request.app.state.db
    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await db_delete_checkpoint(db, checkpoint_id)
    return {"status": "deleted"}
