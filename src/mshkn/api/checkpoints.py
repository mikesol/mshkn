from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.db import delete_checkpoint as db_delete_checkpoint
from mshkn.db import (
    get_active_computer_for_label,
    get_checkpoint,
    insert_checkpoint,
    insert_deferred,
    list_checkpoints_by_account,
)

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.models import Account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])

_require_account = Depends(require_account)


class ForkRequest(BaseModel):
    recipe_id: str | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    exclusive: Literal["error_on_conflict", "defer_on_conflict"] | None = None
    meta_exec: str | None = None


class ForkResponse(BaseModel):
    computer_id: str
    checkpoint_id: str
    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None


@router.post("/{checkpoint_id}/fork", response_model=None)
async def fork_checkpoint(
    checkpoint_id: str,
    request: Request,
    body: ForkRequest | None = None,
    account: Account = _require_account,
) -> ForkResponse | JSONResponse:
    db: aiosqlite.Connection = request.app.state.db

    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    fork_recipe_id = body.recipe_id if body and body.recipe_id else None

    # Exclusive restore: prevent concurrent computers on the same checkpoint chain
    if body and body.exclusive and ckpt.label:
        active = await get_active_computer_for_label(db, account.id, ckpt.label)
        if active is not None:
            if body.exclusive == "error_on_conflict":
                raise HTTPException(
                    status_code=409,
                    detail="Checkpoint chain has active computer",
                )
            if body.exclusive == "defer_on_conflict":
                deferred_id = f"def-{uuid.uuid4().hex[:12]}"
                payload = {
                    "checkpoint_id": checkpoint_id,
                    "recipe_id": body.recipe_id if body else None,
                    "exec": body.exec,
                    "self_destruct": body.self_destruct,
                    "callback_url": body.callback_url,
                    "meta_exec": body.meta_exec,
                }
                now = datetime.now(UTC).isoformat()
                await insert_deferred(
                    db, deferred_id, ckpt.label, account.id,
                    json.dumps(payload), now,
                )
                return JSONResponse(
                    status_code=202,
                    content={"deferred_id": deferred_id, "status": "queued"},
                )

    vm_mgr = request.app.state.vm_manager
    computer = await vm_mgr.fork_from_checkpoint(account.id, ckpt, recipe_id=fork_recipe_id)

    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None

    # Exec on fork
    if body and body.exec is not None:
        from mshkn.api.computers import _get_pool, _self_destruct
        from mshkn.vm.ssh import ssh_exec

        config = request.app.state.config
        pool = _get_pool(request)
        result = await ssh_exec(
            computer.vm_ip, body.exec, config.ssh_key_path, pool=pool,
        )
        exec_exit_code = result.exit_code
        exec_stdout = result.stdout
        exec_stderr = result.stderr

        # Self-destruct: checkpoint + destroy
        if body.self_destruct:
            # Inherit label from source checkpoint
            label = ckpt.label
            created_checkpoint_id = await _self_destruct(
                computer=computer,
                account=account,
                label=label,
                source_checkpoint_id=checkpoint_id,
                exec_exit_code=exec_exit_code,
                exec_stdout=exec_stdout,
                exec_stderr=exec_stderr,
                callback_url=body.callback_url,
                db=db,
                config=config,
                vm_mgr=vm_mgr,
                pool=pool,
            )

    return ForkResponse(
        computer_id=computer.id,
        checkpoint_id=checkpoint_id,
        exec_exit_code=exec_exit_code,
        exec_stdout=exec_stdout,
        exec_stderr=exec_stderr,
        created_checkpoint_id=created_checkpoint_id,
    )


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
    from mshkn.models import Checkpoint
    from mshkn.vm.storage import create_snapshot, mount_volume, umount_volume

    db: aiosqlite.Connection = request.app.state.db
    config = request.app.state.config
    vm_mgr = request.app.state.vm_manager

    # Validate parent checkpoint
    ckpt_parent = await get_checkpoint(db, parent_id)
    if ckpt_parent is None or ckpt_parent.account_id != account.id:
        raise HTTPException(status_code=404, detail="Parent checkpoint not found")

    # Reject self-merge
    if body.checkpoint_a == body.checkpoint_b:
        raise HTTPException(
            status_code=400,
            detail="Cannot merge a checkpoint with itself",
        )

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

        # Mount merged output volume read-write
        # Output starts as a snapshot of parent — we apply the merge diff on top
        await mount_volume(merged_volume_name, mount_output)
        mounted.append(mount_output)

        # Run three-way merge to a temp directory first
        merge_output = Path(f"{merge_dir}/merge_result")
        result = three_way_merge(
            parent=Path(mount_parent),
            fork_a=Path(mount_a),
            fork_b=Path(mount_b),
            output=merge_output,
        )

        # Apply merge result: sync changed/added files to output volume
        # and remove files that were deleted (exist in parent but not in merge)
        for rel in merge_output.rglob("*"):
            if rel.is_file():
                rel_path = rel.relative_to(merge_output)
                dest = Path(mount_output) / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(rel, dest)

        # Remove files that exist in parent but not in merge result (deletions)
        for rel in Path(mount_parent).rglob("*"):
            if rel.is_file():
                rel_path = rel.relative_to(Path(mount_parent))
                if not (merge_output / rel_path).exists():
                    target = Path(mount_output) / rel_path
                    if target.exists():
                        target.unlink()

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
    # Merged checkpoint inherits parent's manifest and recipe
    ckpt = Checkpoint(
        id=checkpoint_id,
        account_id=account.id,
        parent_id=parent_id,
        computer_id=None,  # merge has no source computer
        thin_volume_id=merged_volume_id,
        manifest_hash=ckpt_parent.manifest_hash,
        manifest_json=ckpt_parent.manifest_json,
        r2_prefix=r2_prefix,
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label="merge",
        pinned=False,
        created_at=now,
        recipe_id=ckpt_parent.recipe_id,
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
    label: str | None = None,
    account: Account = _require_account,
) -> list[dict[str, object]]:
    db: aiosqlite.Connection = request.app.state.db
    checkpoints = await list_checkpoints_by_account(db, account.id, label=label)
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

    from mshkn.api.computers import cancel_upload_task
    from mshkn.checkpoint.r2 import delete_checkpoint_r2
    from mshkn.vm.storage import remove_volume

    db: aiosqlite.Connection = request.app.state.db
    config = request.app.state.config
    ckpt = await get_checkpoint(db, checkpoint_id)
    if ckpt is None or ckpt.account_id != account.id:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    # Cancel any in-flight R2 upload before deleting local files
    await cancel_upload_task(checkpoint_id)

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
