from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from mshkn.api.auth import require_account
from mshkn.api.metrics import (
    checkpoints_total,
    computers_active,
    computers_created_total,
    exec_duration_seconds,
)
from mshkn.api.ratelimit import rate_limiter
from mshkn.checkpoint.r2 import upload_checkpoint
from mshkn.checkpoint.snapshot import create_vm_snapshot
from mshkn.db import (
    count_active_computers_by_account,
    get_computer,
    get_latest_checkpoint_for_computer,
    insert_checkpoint,
    update_last_exec_at,
)
from mshkn.models import Checkpoint, Manifest
from mshkn.vm.ssh import (
    ssh_download,
    ssh_exec,
    ssh_exec_bg,
    ssh_exec_stream,
    ssh_gather_metrics,
    ssh_upload,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account, Computer
    from mshkn.vm.manager import VMManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/computers", tags=["computers"])

# Hold references to background tasks to prevent GC
_background_tasks: set[asyncio.Task[None]] = set()

_require_account = Depends(require_account)


class CreateRequest(BaseModel):
    uses: list[str] = []
    needs: dict[str, object] | None = None


class CreateResponse(BaseModel):
    computer_id: str
    url: str
    manifest_hash: str


class ExecRequest(BaseModel):
    command: str


def _check_rate_limit(request: Request) -> None:
    """Check per-API-key rate limit; raise 429 if exceeded."""
    api_key = request.headers.get("Authorization", "")[7:]
    if not rate_limiter.check(api_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


async def _get_running_computer(
    db: aiosqlite.Connection, computer_id: str, account: Account
) -> Computer:
    """Fetch a computer, verify ownership and running status."""
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id:
        raise HTTPException(status_code=404, detail="Computer not found")
    if computer.status != "running":
        raise HTTPException(status_code=400, detail=f"Computer is {computer.status}")
    return computer


@router.post("", response_model=CreateResponse)
async def create_computer(
    request: Request,
    body: CreateRequest,
    account: Account = _require_account,
) -> CreateResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    vm_mgr: VMManager = request.app.state.vm_manager

    active_count = await count_active_computers_by_account(db, account.id)
    if active_count >= account.vm_limit:
        raise HTTPException(status_code=429, detail="VM limit reached")

    manifest = Manifest(uses=body.uses)
    computer = await vm_mgr.create(account.id, manifest, needs=body.needs)
    computers_created_total.inc()
    computers_active.inc()
    return CreateResponse(
        computer_id=computer.id,
        url=f"https://{computer.id}.{config.domain}",
        manifest_hash=computer.manifest_hash,
    )


@router.post("/{computer_id}/exec")
async def exec_command(
    computer_id: str,
    body: ExecRequest,
    request: Request,
    account: Account = _require_account,
) -> EventSourceResponse:
    _check_rate_limit(request)

    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)

    from datetime import UTC, datetime

    await update_last_exec_at(db, computer_id, datetime.now(UTC).isoformat())

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        t0 = time.monotonic()
        try:
            async for stream, line in ssh_exec_stream(
                computer.vm_ip, body.command, config.ssh_key_path
            ):
                yield {"event": stream, "data": line}
        finally:
            exec_duration_seconds.observe(time.monotonic() - t0)

    return EventSourceResponse(event_stream())


@router.post("/{computer_id}/exec/bg")
async def exec_bg(
    computer_id: str,
    body: ExecRequest,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)

    from datetime import UTC, datetime

    await update_last_exec_at(db, computer_id, datetime.now(UTC).isoformat())
    pid = await ssh_exec_bg(computer.vm_ip, body.command, config.ssh_key_path)
    return {"pid": pid}


@router.get("/{computer_id}/exec/logs/{pid}")
async def exec_logs(
    computer_id: str,
    pid: int,
    request: Request,
    account: Account = _require_account,
) -> EventSourceResponse:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        result = await ssh_exec(
            computer.vm_ip,
            f"cat /tmp/bg-{pid}.log 2>/dev/null || echo ''",
            config.ssh_key_path,
            timeout=10.0,
        )
        for line in result.stdout.splitlines():
            yield {"event": "stdout", "data": line}
        yield {"event": "exit", "data": "0"}

    return EventSourceResponse(event_stream())


@router.post("/{computer_id}/exec/kill/{pid}")
async def exec_kill(
    computer_id: str,
    pid: int,
    request: Request,
    account: Account = _require_account,
) -> dict[str, str]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)
    result = await ssh_exec(computer.vm_ip, f"kill {pid}", config.ssh_key_path)
    if result.exit_code != 0:
        return {"status": "not_found", "stderr": result.stderr}
    return {"status": "killed"}


@router.post("/{computer_id}/upload")
async def upload_file(
    computer_id: str,
    request: Request,
    path: str = Query(..., description="Remote file path"),
    account: Account = _require_account,
) -> dict[str, str]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)
    data = await request.body()
    await ssh_upload(computer.vm_ip, path, data, config.ssh_key_path)
    return {"status": "uploaded", "path": path}


@router.get("/{computer_id}/download")
async def download_file(
    computer_id: str,
    request: Request,
    path: str = Query(..., description="Remote file path"),
    account: Account = _require_account,
) -> Response:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)
    data = await ssh_download(computer.vm_ip, path, config.ssh_key_path)
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{computer_id}/status")
async def computer_status(
    computer_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id or computer.status == "destroyed":
        raise HTTPException(status_code=404, detail="Computer not found")
    result: dict[str, object] = {
        "computer_id": computer.id,
        "status": computer.status,
        "url": f"https://{computer.id}.{config.domain}",
        "vm_ip": computer.vm_ip,
        "manifest_hash": computer.manifest_hash,
        "created_at": computer.created_at,
        "last_exec_at": computer.last_exec_at,
    }
    # Enrich with live VM metrics if the VM is running
    if computer.status == "running" and computer.vm_ip:
        try:
            metrics = await ssh_gather_metrics(
                computer.vm_ip, config.ssh_key_path, timeout=10.0,
            )
            result["cpu_pct"] = metrics.cpu_pct
            result["ram_usage_mb"] = metrics.ram_usage_mb
            result["ram_total_mb"] = metrics.ram_total_mb
            result["disk_usage_mb"] = metrics.disk_usage_mb
            result["disk_total_mb"] = metrics.disk_total_mb
            result["processes"] = metrics.processes
        except Exception:
            logger.warning("Failed to gather metrics for %s", computer_id)
    return result


class CheckpointRequest(BaseModel):
    label: str | None = None
    pin: bool = False


class CheckpointResponse(BaseModel):
    checkpoint_id: str
    manifest_hash: str


@router.post("/{computer_id}/checkpoint", response_model=CheckpointResponse)
async def checkpoint_computer(
    computer_id: str,
    request: Request,
    body: CheckpointRequest | None = None,
    account: Account = _require_account,
) -> CheckpointResponse:
    import uuid
    from datetime import UTC, datetime

    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    vm_mgr: VMManager = request.app.state.vm_manager
    computer = await _get_running_computer(db, computer_id, account)

    checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
    snapshot_dir = config.checkpoint_local_dir / checkpoint_id

    # Flush guest filesystem buffers to the block device so the disk
    # snapshot captures all written data (guest page cache is not visible
    # to dm-thin snapshots).
    await ssh_exec(computer.vm_ip, "sync", config.ssh_key_path, timeout=10.0)

    # Pause/snapshot/resume (sub-1s for the agent)
    await create_vm_snapshot(computer.socket_path, snapshot_dir)

    # Freeze disk state: create a dm-thin CoW snapshot so fork gets the disk
    # as it was at checkpoint time, not the computer's evolving state.
    ckpt_volume_id = await vm_mgr.snapshot_disk_for_checkpoint(
        computer, checkpoint_id,
    )

    # Determine parent_id for DAG lineage
    latest = await get_latest_checkpoint_for_computer(db, computer_id)
    if latest is not None:
        parent_id = latest.id
    elif computer.source_checkpoint_id is not None:
        parent_id = computer.source_checkpoint_id
    else:
        parent_id = None

    # Record in DB
    now = datetime.now(UTC).isoformat()
    r2_prefix = f"{account.id}/{checkpoint_id}"
    ckpt = Checkpoint(
        id=checkpoint_id,
        account_id=account.id,
        parent_id=parent_id,
        computer_id=computer_id,
        thin_volume_id=ckpt_volume_id,
        manifest_hash=computer.manifest_hash,
        manifest_json=computer.manifest_json,
        r2_prefix=r2_prefix,
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=body.label if body else None,
        pinned=body.pin if body else False,
        created_at=now,
    )
    await insert_checkpoint(db, ckpt)
    checkpoints_total.inc()

    # Async background upload to R2
    task = asyncio.create_task(upload_checkpoint(snapshot_dir, r2_prefix, config.r2_bucket))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return CheckpointResponse(
        checkpoint_id=checkpoint_id,
        manifest_hash=computer.manifest_hash,
    )


@router.delete("/{computer_id}")
async def destroy_computer(
    request: Request,
    computer_id: str,
    account: Account = _require_account,
) -> dict[str, str]:
    db: aiosqlite.Connection = request.app.state.db
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id:
        raise HTTPException(status_code=404, detail="Computer not found")
    if computer.status == "destroyed":
        raise HTTPException(status_code=404, detail="Computer not found")
    vm_mgr: VMManager = request.app.state.vm_manager
    await vm_mgr.destroy(computer_id)
    computers_active.dec()
    return {"status": "destroyed"}
