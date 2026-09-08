"""Checkpoint endpoints: fork by id or by label (with the exclusive-restore
deferral), merge, list and delete. The work itself lives in mshkn.services.checkpoints."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mshkn.api import scopes
from mshkn.api.deps import get_runtime, require_principal
from mshkn.api.schemas import (
    CheckpointSummary,
    DeferredResponse,
    DeleteResponse,
    ForkByLabelRequest,
    ForkRequest,
    ForkResponse,
    MergeConflict,
    MergeRequest,
    MergeResponse,
    fork_response,
)
from mshkn.models import ExecSpec
from mshkn.services.checkpoints import Deferred

if TYPE_CHECKING:
    from mshkn.models import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"])

_require_principal = Depends(require_principal)


def _deferred(forked: Deferred) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content=DeferredResponse(deferred_id=forked.deferred_id, status="queued").model_dump(),
    )


# Declared before /{checkpoint_id}/fork so "fork" is not read as a checkpoint id.
@router.post(
    "/fork",
    response_model=ForkResponse,
    responses={202: {"model": DeferredResponse}},
)
async def fork_by_label(
    body: ForkByLabelRequest,
    request: Request,
    principal: Principal = _require_principal,
) -> ForkResponse | JSONResponse:
    """Advance a labelled chain: fork its newest checkpoint, atomically."""
    account = principal.account
    scopes.require_label(principal, body.label)
    rt = get_runtime(request)
    spec = ExecSpec(
        command=body.exec,
        self_destruct=body.self_destruct,
        callback_url=body.callback_url,
        label=None,
        meta_exec=body.meta_exec,
    )
    head, forked = await rt.checkpoints.fork_by_label(
        account,
        body.label,
        spec,
        exclusive=body.exclusive,
        recipe_id=body.recipe_id,
        api_key_id=scopes.key_id(principal),
    )
    if isinstance(forked, Deferred):
        return _deferred(forked)
    outcome = await rt.lifecycle.run_ephemeral(account, forked, spec, source_checkpoint=head)
    return fork_response(forked, head.id, outcome)


@router.post(
    "/{checkpoint_id}/fork",
    response_model=ForkResponse,
    responses={202: {"model": DeferredResponse}},
)
async def fork_checkpoint(
    checkpoint_id: str,
    request: Request,
    body: ForkRequest | None = None,
    principal: Principal = _require_principal,
) -> ForkResponse | JSONResponse:
    account = principal.account
    rt = get_runtime(request)
    body = body or ForkRequest()
    ckpt = await rt.checkpoints.get_owned(account, checkpoint_id)
    scopes.require_checkpoint_label(principal, ckpt)
    spec = ExecSpec(
        command=body.exec,
        self_destruct=body.self_destruct,
        callback_url=body.callback_url,
        label=None,
        meta_exec=body.meta_exec,
    )
    forked = await rt.checkpoints.fork_or_defer(
        account,
        ckpt,
        spec,
        recipe_id=body.recipe_id,
        exclusive=body.exclusive,
        api_key_id=scopes.key_id(principal),
    )
    if isinstance(forked, Deferred):
        return _deferred(forked)
    outcome = await rt.lifecycle.run_ephemeral(account, forked, spec, source_checkpoint=ckpt)
    return fork_response(forked, ckpt.id, outcome)


@router.post("/{parent_id}/merge", response_model=MergeResponse)
async def merge_checkpoints(
    parent_id: str,
    body: MergeRequest,
    request: Request,
    principal: Principal = _require_principal,
) -> MergeResponse:
    account = principal.account
    scopes.require_account_key(principal)
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
    principal: Principal = _require_principal,
) -> list[CheckpointSummary]:
    account = principal.account
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
        for c in scopes.visible_checkpoints(
            principal, await rt.checkpoints.list(account, label=label)
        )
    ]


@router.delete("/{checkpoint_id}", response_model=DeleteResponse)
async def delete_checkpoint(
    checkpoint_id: str,
    request: Request,
    principal: Principal = _require_principal,
) -> DeleteResponse:
    account = principal.account
    rt = get_runtime(request)
    ckpt = await rt.checkpoints.get_owned(account, checkpoint_id)
    scopes.require_checkpoint_label(principal, ckpt)
    await rt.checkpoints.delete(ckpt)
    return DeleteResponse(status="deleted")
