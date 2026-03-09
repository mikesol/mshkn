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

    vm_mgr = request.app.state.vm_manager
    computer = await vm_mgr.fork_from_checkpoint(account.id, ckpt)
    return ForkResponse(computer_id=computer.id, checkpoint_id=checkpoint_id)


class MergeRequest(BaseModel):
    checkpoint_a: str
    checkpoint_b: str


@router.post("/merge")
async def merge_checkpoints(
    body: MergeRequest,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    db: aiosqlite.Connection = request.app.state.db

    ckpt_a = await get_checkpoint(db, body.checkpoint_a)
    ckpt_b = await get_checkpoint(db, body.checkpoint_b)

    if ckpt_a is None or ckpt_a.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint A not found")
    if ckpt_b is None or ckpt_b.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint B not found")

    if ckpt_a.parent_id != ckpt_b.parent_id:
        raise HTTPException(
            status_code=400,
            detail="Checkpoints must share a common parent to merge",
        )

    # TODO: Full merge path:
    # 1. Mount disk volumes for parent, fork_a, fork_b
    # 2. Run three_way_merge on mounted filesystems
    # 3. Create new checkpoint from merged result
    # 4. Upload to R2
    # For now, return a placeholder showing the intended structure
    return {
        "status": "pending",
        "checkpoint_a": body.checkpoint_a,
        "checkpoint_b": body.checkpoint_b,
        "parent_id": ckpt_a.parent_id,
        "message": "Merge requires server-side disk mount — not yet implemented",
    }


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
            "checkpoint_id": c.id,
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
    from mshkn.vm.storage import remove_volume

    db: aiosqlite.Connection = request.app.state.db
    config = request.app.state.config
    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    # Clean up dm-thin volume if one was allocated
    if ckpt.thin_volume_id is not None:
        volume_name = f"mshkn-ckpt-{checkpoint_id}"
        await remove_volume(config.thin_pool_name, volume_name, ckpt.thin_volume_id)

    await db_delete_checkpoint(db, checkpoint_id)
    return {"status": "deleted"}
