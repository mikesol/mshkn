"""Computer endpoints. Every handler resolves the runtime, calls one service
method, and shapes the result; the orchestration lives in mshkn.services."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sse_starlette.sse import EventSourceResponse

from mshkn.api.deps import get_runtime, require_account
from mshkn.api.schemas import (
    CheckpointRequest,
    CheckpointResponse,
    ComputerStatusResponse,
    CreateRequest,
    CreateResponse,
    DestroyResponse,
    ExecBgResponse,
    ExecKillResponse,
    ExecRequest,
    UploadResponse,
    create_response,
)
from mshkn.models import CheckpointTrigger, ComputerStatus, ExecSpec
from mshkn.observability.metrics import exec_duration_seconds
from mshkn.resources import Resources

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mshkn.models import Account
    from mshkn.runtime import Runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/computers", tags=["computers"])

_require_account = Depends(require_account)


def _check_rate_limit(rt: Runtime, request: Request) -> None:
    """Check per-API-key rate limit; raise 429 if exceeded."""
    api_key = request.headers.get("Authorization", "")[7:]
    if not rt.rate_limiter.check(api_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


@router.post("", response_model=CreateResponse)
async def create_computer(
    request: Request,
    body: CreateRequest,
    account: Account = _require_account,
) -> CreateResponse:
    rt = get_runtime(request)
    resources = Resources.from_needs(body.needs)
    computer = await rt.computers.create(account, recipe_id=body.recipe_id, resources=resources)
    spec = ExecSpec(
        command=body.exec,
        self_destruct=body.self_destruct,
        callback_url=body.callback_url,
        label=body.label,
        meta_exec=body.meta_exec,
    )
    outcome = await rt.lifecycle.run_ephemeral(account, computer, spec, source_checkpoint=None)
    return create_response(computer, outcome, domain=rt.config.domain)


@router.post("/{computer_id}/exec")
async def exec_command(
    computer_id: str,
    body: ExecRequest,
    request: Request,
    account: Account = _require_account,
) -> EventSourceResponse:
    rt = get_runtime(request)
    _check_rate_limit(rt, request)
    computer = await rt.computers.get_running(account, computer_id)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        t0 = time.monotonic()
        try:
            async for stream, line in rt.computers.stream(computer, body.command):
                yield {"event": stream, "data": line}
        except Exception as exc:
            logger.warning("exec stream for %s failed: %s", computer_id, type(exc).__name__)
            yield {"event": "error", "data": f"{type(exc).__name__}: {exc}"}
            yield {"event": "exit", "data": "255"}
        finally:
            exec_duration_seconds.observe(time.monotonic() - t0)

    return EventSourceResponse(event_stream())


@router.post("/{computer_id}/exec/bg", response_model=ExecBgResponse)
async def exec_bg(
    computer_id: str,
    body: ExecRequest,
    request: Request,
    account: Account = _require_account,
) -> ExecBgResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_running(account, computer_id)
    return ExecBgResponse(pid=await rt.computers.exec_bg(computer, body.command))


@router.get("/{computer_id}/exec/logs/{pid}")
async def exec_logs(
    computer_id: str,
    pid: int,
    request: Request,
    account: Account = _require_account,
) -> EventSourceResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_running(account, computer_id)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        for line in await rt.computers.exec_logs(computer, pid):
            yield {"event": "stdout", "data": line}
        yield {"event": "exit", "data": "0"}

    return EventSourceResponse(event_stream())


@router.post("/{computer_id}/exec/kill/{pid}", response_model=ExecKillResponse)
async def exec_kill(
    computer_id: str,
    pid: int,
    request: Request,
    account: Account = _require_account,
) -> ExecKillResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_running(account, computer_id)
    result = await rt.computers.exec_kill(computer, pid)
    if result.exit_code != 0:
        return ExecKillResponse(status="not_found", stderr=result.stderr)
    return ExecKillResponse(status="killed")


@router.post("/{computer_id}/upload", response_model=UploadResponse)
async def upload_file(
    computer_id: str,
    request: Request,
    path: str = Query(..., description="Remote file path"),
    account: Account = _require_account,
) -> UploadResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_running(account, computer_id)
    await rt.computers.upload(computer, path, await request.body())
    return UploadResponse(status="uploaded", path=path)


@router.get("/{computer_id}/download")
async def download_file(
    computer_id: str,
    request: Request,
    path: str = Query(..., description="Remote file path"),
    account: Account = _require_account,
) -> Response:
    rt = get_runtime(request)
    computer = await rt.computers.get_running(account, computer_id)
    data = await rt.computers.download(computer, path)
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{computer_id}/status", response_model=ComputerStatusResponse)
async def computer_status(
    computer_id: str,
    request: Request,
    account: Account = _require_account,
) -> ComputerStatusResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_owned(account, computer_id)
    response = ComputerStatusResponse(
        computer_id=computer.id,
        status=computer.status,
        url=f"https://{computer.id}.{rt.config.domain}",
        vm_ip=computer.vm_ip,
        recipe_id=computer.recipe_id,
        created_at=computer.created_at,
        last_exec_at=computer.last_exec_at,
    )
    # Enrich with live VM metrics only if there is a VM to ask.
    if computer.status == ComputerStatus.RUNNING and computer.vm_ip:
        metrics = await rt.computers.metrics(computer)
        if metrics is not None:
            response.cpu_pct = metrics.cpu_pct
            response.ram_usage_mb = metrics.ram_usage_mb
            response.ram_total_mb = metrics.ram_total_mb
            response.disk_usage_mb = metrics.disk_usage_mb
            response.disk_total_mb = metrics.disk_total_mb
            response.processes = metrics.processes
    return response


@router.post("/{computer_id}/checkpoint", response_model=CheckpointResponse)
async def checkpoint_computer(
    computer_id: str,
    request: Request,
    body: CheckpointRequest | None = None,
    account: Account = _require_account,
) -> CheckpointResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_running(account, computer_id)
    ckpt = await rt.checkpoints.create(
        computer,
        label=body.label if body else None,
        pin=body.pin if body else False,
        trigger=CheckpointTrigger.API,
    )
    return CheckpointResponse(checkpoint_id=ckpt.id, recipe_id=ckpt.recipe_id)


@router.delete("/{computer_id}", response_model=DestroyResponse)
async def destroy_computer(
    request: Request,
    computer_id: str,
    account: Account = _require_account,
) -> DestroyResponse:
    rt = get_runtime(request)
    computer = await rt.computers.get_owned(account, computer_id)
    await rt.computers.destroy(computer.id)
    await rt.lifecycle.drain_after_destroy(account, computer)
    return DestroyResponse(status=ComputerStatus.DESTROYED)
