from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.db import delete_checkpoint as db_delete_checkpoint
from mshkn.db import get_checkpoint, insert_checkpoint, list_checkpoints_by_account

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.models import Account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])

_require_account = Depends(require_account)


class ForkRequest(BaseModel):
    manifest: dict[str, object] | None = None
    skip_manifest_check: bool = False


class ForkResponse(BaseModel):
    computer_id: str
    checkpoint_id: str


def _is_manifest_additive(parent_uses: list[str], new_uses: list[str]) -> bool:
    """Check if new manifest is a superset of parent (additive change)."""
    return set(parent_uses).issubset(set(new_uses))


@router.post("/{checkpoint_id}/fork", response_model=ForkResponse)
async def fork_checkpoint(
    checkpoint_id: str,
    request: Request,
    body: ForkRequest | None = None,
    account: Account = _require_account,
) -> ForkResponse:
    from mshkn.models import Manifest

    db: aiosqlite.Connection = request.app.state.db

    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    # Determine manifest for fork
    if body and body.manifest and "uses" in body.manifest:
        raw_uses = body.manifest["uses"]
        if not isinstance(raw_uses, list):
            raise HTTPException(status_code=422, detail="uses must be a list")
        new_uses = [str(u) for u in raw_uses]
        parent_manifest = Manifest.from_json(ckpt.manifest_json)

        is_breaking = not _is_manifest_additive(parent_manifest.uses, new_uses)
        if is_breaking and not body.skip_manifest_check:
            raise HTTPException(
                status_code=409,
                detail="Breaking manifest change (removal or version change). "
                       "Set skip_manifest_check: true to proceed anyway.",
            )

        fork_manifest = Manifest(uses=new_uses)
    else:
        fork_manifest = Manifest.from_json(ckpt.manifest_json)

    vm_mgr = request.app.state.vm_manager
    computer = await vm_mgr.fork_from_checkpoint(account.id, ckpt, fork_manifest)
    return ForkResponse(computer_id=computer.id, checkpoint_id=checkpoint_id)


class MergeRequest(BaseModel):
    checkpoint_a: str
    checkpoint_b: str


@router.post("/{parent_id}/merge")
async def merge_checkpoints(
    parent_id: str,
    body: MergeRequest,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    import shutil
    import tempfile
    from pathlib import Path

    from mshkn.checkpoint.merge import three_way_merge
    from mshkn.models import Checkpoint, Manifest
    from mshkn.vm.storage import create_snapshot, mount_volume, umount_volume

    db: aiosqlite.Connection = request.app.state.db
    config = request.app.state.config
    vm_mgr = request.app.state.vm_manager

    # Validate parent checkpoint
    ckpt_parent = await get_checkpoint(db, parent_id)
    if ckpt_parent is None or ckpt_parent.account_id != account.id:
        raise HTTPException(status_code=404, detail="Parent checkpoint not found")

    # Validate fork checkpoints
    ckpt_a = await get_checkpoint(db, body.checkpoint_a)
    ckpt_b = await get_checkpoint(db, body.checkpoint_b)

    if ckpt_a is None or ckpt_a.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint A not found")
    if ckpt_b is None or ckpt_b.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint B not found")

    # Verify both forks descend from the given parent
    if ckpt_a.parent_id != parent_id or ckpt_b.parent_id != parent_id:
        raise HTTPException(
            status_code=400,
            detail="Both checkpoints must be children of the specified parent",
        )

    # All three must have thin volumes
    for label, ckpt in [("Parent", ckpt_parent), ("A", ckpt_a), ("B", ckpt_b)]:
        if ckpt.thin_volume_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"{label} checkpoint has no disk snapshot",
            )

    # Create mount points
    merge_dir = tempfile.mkdtemp(prefix="mshkn-merge-")
    mount_parent = f"{merge_dir}/parent"
    mount_a = f"{merge_dir}/fork_a"
    mount_b = f"{merge_dir}/fork_b"
    mount_output = f"{merge_dir}/output"

    # Volume names for the three existing checkpoints
    vol_parent = f"mshkn-ckpt-{parent_id}"
    vol_a = f"mshkn-ckpt-{body.checkpoint_a}"
    vol_b = f"mshkn-ckpt-{body.checkpoint_b}"

    # Create a new volume for the merged output (snapshot of parent)
    checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
    async with vm_mgr._alloc_lock:
        merged_volume_id = vm_mgr._allocate_volume_id()
    merged_volume_name = f"mshkn-ckpt-{checkpoint_id}"

    assert ckpt_parent.thin_volume_id is not None  # validated above
    await create_snapshot(
        pool_name=config.thin_pool_name,
        source_volume_id=ckpt_parent.thin_volume_id,
        new_volume_id=merged_volume_id,
        new_volume_name=merged_volume_name,
        sectors=config.thin_volume_sectors,
    )

    mounted: list[str] = []
    try:
        # Mount source volumes read-only
        for vol, mnt in [(vol_parent, mount_parent), (vol_a, mount_a), (vol_b, mount_b)]:
            await mount_volume(vol, mnt, readonly=True)
            mounted.append(mnt)

        # Mount merged output volume read-write and clear it
        # (it starts as a snapshot of parent, but merge populates from scratch)
        await mount_volume(merged_volume_name, mount_output)
        mounted.append(mount_output)
        for item in Path(mount_output).iterdir():
            if item.name == "lost+found":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        # Run three-way merge
        result = three_way_merge(
            parent=Path(mount_parent),
            fork_a=Path(mount_a),
            fork_b=Path(mount_b),
            output=Path(mount_output),
        )

    finally:
        # Always unmount everything
        for mnt in reversed(mounted):
            try:
                await umount_volume(mnt)
            except Exception:
                logger.warning("Failed to unmount %s during merge cleanup", mnt)

        # Clean up temp dir
        shutil.rmtree(merge_dir, ignore_errors=True)

    # Build conflict info for response
    conflicts = [
        {"path": c.path, "resolution": "fork_a"}
        for c in result.conflicts
    ]

    # Create checkpoint record
    now = datetime.now(UTC).isoformat()
    r2_prefix = f"{account.id}/{checkpoint_id}"
    # Merged checkpoint inherits parent's manifest
    parent_manifest = Manifest.from_json(ckpt_parent.manifest_json)
    ckpt = Checkpoint(
        id=checkpoint_id,
        account_id=account.id,
        parent_id=parent_id,
        computer_id=None,  # merge has no source computer
        thin_volume_id=merged_volume_id,
        manifest_hash=parent_manifest.content_hash(),
        manifest_json=ckpt_parent.manifest_json,
        r2_prefix=r2_prefix,
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label="merge",
        pinned=False,
        created_at=now,
    )
    await insert_checkpoint(db, ckpt)

    # Background upload to R2 (snapshot dir doesn't exist for merges,
    # but the volume itself is the checkpoint — skip R2 for now)
    logger.info(
        "Merged checkpoint %s: auto_merged=%d, unchanged=%d, conflicts=%d",
        checkpoint_id, result.auto_merged, result.unchanged, len(result.conflicts),
    )

    return {
        "checkpoint_id": checkpoint_id,
        "conflicts": conflicts,
        "auto_merged": result.auto_merged,
        "unchanged": result.unchanged,
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
    import shutil

    from mshkn.checkpoint.r2 import delete_checkpoint_r2
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

    # Clean up local snapshot directory
    local_dir = config.checkpoint_local_dir / checkpoint_id
    if local_dir.exists():
        shutil.rmtree(local_dir)

    # Clean up R2 uploads
    await delete_checkpoint_r2(ckpt.r2_prefix, config.r2_bucket)

    await db_delete_checkpoint(db, checkpoint_id)
    return {"status": "deleted"}
