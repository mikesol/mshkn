"""The relay's routes (#110): submit a job, read it back. The work is in
mshkn.services.relay; a scoped key is checked here first (§1a)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request

from mshkn.api import scopes
from mshkn.api.deps import get_runtime, require_principal
from mshkn.api.schemas import (
    RelayAcceptedResponse,
    RelayJobResponse,
    RelayRequestBody,
    relay_job_response,
)
from mshkn.models import RelayDelivery, RetryPolicy
from mshkn.services.relay import RelayRequest

if TYPE_CHECKING:
    from mshkn.models import Principal

router = APIRouter(prefix="/relay", tags=["relay"])

_require_principal = Depends(require_principal)


@router.post("", response_model=RelayAcceptedResponse, status_code=202)
async def submit_job(
    body: RelayRequestBody,
    request: Request,
    principal: Principal = _require_principal,
) -> RelayAcceptedResponse:
    scopes.require_relay(principal, body.target, deliver_in_body=body.deliver is not None)
    if principal.scopes is not None:
        deliver = principal.scopes.relay_deliver
    elif body.deliver is not None:
        deliver = RelayDelivery(label=body.deliver.label, exec=body.deliver.exec)
    else:
        deliver = None
    job = await get_runtime(request).relay.submit(
        principal.account,
        RelayRequest(
            target=body.target,
            method=body.method,
            forward_headers=body.forward_headers,
            body=body.body,
            retry=RetryPolicy(
                attempts=body.retry.attempts,
                initial_delay_ms=body.retry.initial_delay_ms,
                max_delay_ms=body.retry.max_delay_ms,
            ),
            timeout_seconds=body.timeout_seconds,
            deliver=deliver,
        ),
        api_key_id=scopes.key_id(principal),
    )
    return RelayAcceptedResponse(job_id=job.id, status=str(job.status))


@router.get("/{job_id}", response_model=RelayJobResponse)
async def get_job(
    job_id: str, request: Request, principal: Principal = _require_principal
) -> RelayJobResponse:
    job = await get_runtime(request).relay.get_owned(principal.account, job_id)
    scopes.require_relay_job_owner(principal, job)
    return relay_job_response(job)
