"""Checkpoint endpoints: fork (with the exclusive-restore deferral), merge,
list and delete. The work itself lives in mshkn.services.checkpoints."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mshkn.api.deps import get_runtime, require_account
from mshkn.api.schemas import (
    CheckpointSummary,
    DeferredResponse,
    DeleteResponse,
    ForkRequest,
    ForkResponse,
    MergeConflict,
    MergeRequest,
    MergeResponse,
)
from mshkn.models import ExecSpec
from mshkn.services.checkpoints import Deferred

if TYPE_CHECKING:
    from mshkn.models import Account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])

_require_account = Depends(require_account)


@router.post("/{checkpoint_id}/fork", response_model=None)
async def fork_checkpoint(
    checkpoint_id: str,
    request: Request,
    body: ForkRequest | None = None,
    account: Account = _require_account,
) -> ForkResponse | JSONResponse:
    rt = get_runtime(request)
    body = body or ForkRequest()
    ckpt = await rt.checkpoints.get_owned(account, checkpoint_id)
    spec = ExecSpec(
        command=body.exec,
        self_destruct=body.self_destruct,
        callback_url=body.callback_url,
        label=None,
        meta_exec=body.meta_exec,
    )
    forked = await rt.checkpoints.fork_or_defer(
        account, ckpt, spec, recipe_id=body.recipe_id, exclusive=body.exclusive
    )
    if isinstance(forked, Deferred):
        return JSONResponse(
            status_code=202,
            content=DeferredResponse(deferred_id=forked.deferred_id, status="queued").model_dump(),
        )
    outcome = await rt.lifecycle.run_ephemeral(account, forked, spec, source_checkpoint=ckpt)
    return ForkResponse(
        computer_id=forked.id,
        checkpoint_id=checkpoint_id,
        exec_exit_code=outcome.exec_exit_code,
        exec_stdout=outcome.exec_stdout,
        exec_stderr=outcome.exec_stderr,
        created_checkpoint_id=outcome.created_checkpoint_id,
    )


@router.post("/{parent_id}/merge", response_model=MergeResponse)
async def merge_checkpoints(
    parent_id: str,
    body: MergeRequest,
    request: Request,
    account: Account = _require_account,
) -> MergeResponse:
    rt = get_runtime(request)
    outcome = await rt.checkpoints.merge(account, parent_id, body.checkpoint_a, body.checkpoint_b)
    return MergeResponse(
        checkpoint_id=outcome.checkpoint.id,
        conflicts=[MergeConflict(path=p, resolution="fork_a") for p in outcome.conflicts],
        auto_merged=outcome.auto_merged,
        unchanged=outcome.unchanged,
    )


@router.get("", response_model=list[CheckpointSummary])
async def list_checkpoints(
    request: Request,
    label: str | None = None,
    account: Account = _require_account,
) -> list[CheckpointSummary]:
    rt = get_runtime(request)
    return [
        CheckpointSummary(
            id=c.id,
            checkpoint_id=c.id,
            parent_id=c.parent_id,
            computer_id=c.computer_id,
            recipe_id=c.recipe_id,
            r2_prefix=c.r2_prefix,
            disk_delta_size_bytes=c.disk_delta_size_bytes,
            memory_size_bytes=c.memory_size_bytes,
            label=c.label,
            pinned=c.pinned,
            created_at=c.created_at,
        )
        for c in await rt.checkpoints.list(account, label=label)
    ]


@router.delete("/{checkpoint_id}", response_model=DeleteResponse)
async def delete_checkpoint(
    checkpoint_id: str,
    request: Request,
    account: Account = _require_account,
) -> DeleteResponse:
    rt = get_runtime(request)
    ckpt = await rt.checkpoints.get_owned(account, checkpoint_id)
    await rt.checkpoints.delete(ckpt)
    return DeleteResponse(status="deleted")
