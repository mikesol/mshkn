from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from mshkn.api.deps import get_runtime, require_account
from mshkn.db import (
    claim_deferred_by_label,
    count_active_computers_by_account,
    get_checkpoint,
    get_computer,
    get_latest_checkpoint_for_computer,
    insert_checkpoint,
    update_last_exec_at,
)
from mshkn.models import Checkpoint, ComputerStatus
from mshkn.observability.metrics import (
    checkpoints_total,
    computers_active,
    computers_created_total,
    exec_duration_seconds,
    timed,
)
from mshkn.resources import Resources
from mshkn.services.callback import deliver_callback

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host import Host
    from mshkn.models import Account, Computer, DeferredRequest
    from mshkn.runtime import BackgroundTasks, Runtime
    from mshkn.vm.manager import VMManager

logger = logging.getLogger(__name__)

STATUS_METRICS_TIMEOUT_SECONDS = 15.0

router = APIRouter(prefix="/computers", tags=["computers"])


_require_account = Depends(require_account)


class CreateRequest(BaseModel):
    recipe_id: str | None = None
    needs: dict[str, object] | None = None
    exec: str | None = None
    self_destruct: bool = False
    callback_url: str | None = None
    label: str | None = None
    meta_exec: str | None = None


class CreateResponse(BaseModel):
    computer_id: str
    url: str
    recipe_id: str | None = None
    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None


class ExecRequest(BaseModel):
    command: str


def _check_rate_limit(rt: Runtime, request: Request) -> None:
    """Check per-API-key rate limit; raise 429 if exceeded."""
    api_key = request.headers.get("Authorization", "")[7:]
    if not rt.rate_limiter.check(api_key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


async def _self_destruct(
    *,
    computer: Computer,
    account: Account,
    label: str | None,
    source_checkpoint_id: str | None,
    exec_exit_code: int,
    exec_stdout: str,
    exec_stderr: str,
    callback_url: str | None,
    db: aiosqlite.Connection,
    config: Config,
    vm_mgr: VMManager,
    host: Host,
    tasks: BackgroundTasks,
) -> str:
    """Auto-checkpoint, destroy computer, and fire callback.

    Returns the created checkpoint ID.
    """
    import uuid
    from datetime import UTC, datetime

    checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
    snapshot_dir = config.checkpoint_local_dir / checkpoint_id

    # Flush guest filesystem
    await host.guest.exec(computer.vm_ip, "sync", timeout=10.0)

    # Pause/snapshot/resume
    await host.hypervisor.snapshot(computer.socket_path, snapshot_dir)

    # Evict SSH pool connection
    await host.guest.evict(computer.vm_ip)

    # Freeze disk
    ckpt_volume_id = await vm_mgr.snapshot_disk_for_checkpoint(
        computer,
        checkpoint_id,
    )

    # Determine parent_id for DAG lineage
    latest = await get_latest_checkpoint_for_computer(db, computer.id)
    if latest is not None:
        parent_id = latest.id
    elif computer.source_checkpoint_id is not None:
        parent_id = computer.source_checkpoint_id
    else:
        parent_id = None

    # Record checkpoint in DB
    now = datetime.now(UTC).isoformat()
    r2_prefix = f"{account.id}/{checkpoint_id}"
    ckpt = Checkpoint(
        id=checkpoint_id,
        account_id=account.id,
        parent_id=parent_id,
        computer_id=computer.id,
        thin_volume_id=ckpt_volume_id,
        r2_prefix=r2_prefix,
        disk_delta_size_bytes=None,
        memory_size_bytes=None,
        label=label,
        pinned=False,
        created_at=now,
        recipe_id=computer.recipe_id,
    )
    await insert_checkpoint(db, ckpt)
    checkpoints_total.labels(trigger="self_destruct").inc()

    # Background R2 upload
    tasks.spawn(
        host.objects.upload_dir(snapshot_dir, r2_prefix),
        name=f"upload:{checkpoint_id}",
        key=f"upload:{checkpoint_id}",
    )

    # Destroy the computer
    await vm_mgr.destroy(computer.id)

    # Fire callback
    if callback_url:
        payload = {
            "computer_id": computer.id,
            "checkpoint_id": source_checkpoint_id,
            "label": label,
            "exec_exit_code": exec_exit_code,
            "exec_stdout": exec_stdout,
            "exec_stderr": exec_stderr,
            "created_checkpoint_id": checkpoint_id,
        }
        tasks.spawn(
            deliver_callback(httpx.AsyncClient(), callback_url, payload),
            name=f"callback:{computer.id}",
        )

    logger.info(
        "Self-destruct: computer %s checkpointed as %s and destroyed",
        computer.id,
        checkpoint_id,
    )

    # Drain deferred queue for this label
    if label:
        deferred = await claim_deferred_by_label(db, label)
        if deferred:
            tasks.spawn(
                _process_deferred(
                    label=label,
                    deferred_items=deferred,
                    db=db,
                    config=config,
                    vm_mgr=vm_mgr,
                    account=account,
                    host=host,
                    tasks=tasks,
                ),
                name=f"deferred:{label}",
            )

    return checkpoint_id


async def _get_running_computer(
    db: aiosqlite.Connection, computer_id: str, account: Account
) -> Computer:
    """Fetch a computer, verify ownership and running status."""
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id:
        raise HTTPException(status_code=404, detail="Computer not found")
    if computer.status != ComputerStatus.RUNNING:
        raise HTTPException(status_code=400, detail=f"Computer is {computer.status}")
    return computer


@router.post("", response_model=CreateResponse)
async def create_computer(
    request: Request,
    body: CreateRequest,
    account: Account = _require_account,
) -> CreateResponse:
    rt = get_runtime(request)
    db = rt.db
    config = rt.config
    vm_mgr = rt.vm_manager

    active_count = await count_active_computers_by_account(db, account.id)
    if active_count >= account.vm_limit:
        raise HTTPException(status_code=429, detail="VM limit reached")

    resources = Resources.from_needs(body.needs)
    async with timed("create"):
        computer = await vm_mgr.create(account.id, recipe_id=body.recipe_id, resources=resources)
    computers_created_total.labels(source="create").inc()
    computers_active.inc()

    exec_exit_code: int | None = None
    exec_stdout: str | None = None
    exec_stderr: str | None = None
    created_checkpoint_id: str | None = None

    # Exec on create
    if body.exec is not None:
        result = await rt.host.guest.exec(computer.vm_ip, body.exec)
        exec_exit_code = result.exit_code
        exec_stdout = result.stdout
        exec_stderr = result.stderr

        # Self-destruct: checkpoint + destroy
        if body.self_destruct:
            created_checkpoint_id = await _self_destruct(
                computer=computer,
                account=account,
                label=body.label,
                source_checkpoint_id=None,
                exec_exit_code=exec_exit_code,
                exec_stdout=exec_stdout,
                exec_stderr=exec_stderr,
                callback_url=body.callback_url,
                db=db,
                config=config,
                vm_mgr=vm_mgr,
                host=rt.host,
                tasks=rt.tasks,
            )
            computers_active.dec()

    return CreateResponse(
        computer_id=computer.id,
        url=f"https://{computer.id}.{config.domain}",
        recipe_id=computer.recipe_id,
        exec_exit_code=exec_exit_code,
        exec_stdout=exec_stdout,
        exec_stderr=exec_stderr,
        created_checkpoint_id=created_checkpoint_id,
    )


@router.post("/{computer_id}/exec")
async def exec_command(
    computer_id: str,
    body: ExecRequest,
    request: Request,
    account: Account = _require_account,
) -> EventSourceResponse:
    rt = get_runtime(request)
    _check_rate_limit(rt, request)

    db = rt.db
    computer = await _get_running_computer(db, computer_id, account)

    from datetime import UTC, datetime

    await update_last_exec_at(db, computer_id, datetime.now(UTC).isoformat())

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        t0 = time.monotonic()
        try:
            async for stream, line in rt.host.guest.stream(computer.vm_ip, body.command):
                yield {"event": stream, "data": line}
        except Exception as exc:
            logger.warning("exec stream for %s failed: %s", computer_id, type(exc).__name__)
            yield {"event": "error", "data": f"{type(exc).__name__}: {exc}"}
            yield {"event": "exit", "data": "255"}
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
    rt = get_runtime(request)
    db = rt.db
    computer = await _get_running_computer(db, computer_id, account)

    from datetime import UTC, datetime

    await update_last_exec_at(db, computer_id, datetime.now(UTC).isoformat())
    pid = await rt.host.guest.exec_bg(computer.vm_ip, body.command)
    return {"pid": pid}


@router.get("/{computer_id}/exec/logs/{pid}")
async def exec_logs(
    computer_id: str,
    pid: int,
    request: Request,
    account: Account = _require_account,
) -> EventSourceResponse:
    rt = get_runtime(request)
    db = rt.db
    computer = await _get_running_computer(db, computer_id, account)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        result = await rt.host.guest.exec(
            computer.vm_ip,
            f"cat /tmp/bg-{pid}.log 2>/dev/null || echo ''",
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
    rt = get_runtime(request)
    db = rt.db
    computer = await _get_running_computer(db, computer_id, account)
    result = await rt.host.guest.exec(computer.vm_ip, f"kill {pid}")
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
    rt = get_runtime(request)
    db = rt.db
    computer = await _get_running_computer(db, computer_id, account)
    data = await request.body()
    await rt.host.guest.upload(computer.vm_ip, path, data)
    return {"status": "uploaded", "path": path}


@router.get("/{computer_id}/download")
async def download_file(
    computer_id: str,
    request: Request,
    path: str = Query(..., description="Remote file path"),
    account: Account = _require_account,
) -> Response:
    rt = get_runtime(request)
    db = rt.db
    computer = await _get_running_computer(db, computer_id, account)
    data = await rt.host.guest.download(computer.vm_ip, path)
    return Response(content=data, media_type="application/octet-stream")


@router.get("/{computer_id}/status")
async def computer_status(
    computer_id: str,
    request: Request,
    account: Account = _require_account,
) -> dict[str, object]:
    rt = get_runtime(request)
    db = rt.db
    config = rt.config
    computer = await get_computer(db, computer_id)
    if (
        computer is None
        or computer.account_id != account.id
        or computer.status == ComputerStatus.DESTROYED
    ):
        raise HTTPException(status_code=404, detail="Computer not found")
    result: dict[str, object] = {
        "computer_id": computer.id,
        "status": computer.status,
        "url": f"https://{computer.id}.{config.domain}",
        "vm_ip": computer.vm_ip,
        "recipe_id": computer.recipe_id,
        "created_at": computer.created_at,
        "last_exec_at": computer.last_exec_at,
    }
    # Enrich with live VM metrics if the VM is running
    if computer.status == ComputerStatus.RUNNING and computer.vm_ip:
        try:
            metrics = await asyncio.wait_for(
                rt.host.guest.metrics(computer.vm_ip, timeout=10.0),
                timeout=STATUS_METRICS_TIMEOUT_SECONDS,
            )
            result["cpu_pct"] = metrics.cpu_pct
            result["ram_usage_mb"] = metrics.ram_usage_mb
            result["ram_total_mb"] = metrics.ram_total_mb
            result["disk_usage_mb"] = metrics.disk_usage_mb
            result["disk_total_mb"] = metrics.disk_total_mb
            result["processes"] = metrics.processes
        except Exception as exc:
            logger.warning("Failed to gather metrics for %s: %s", computer_id, type(exc).__name__)
    return result


class CheckpointRequest(BaseModel):
    label: str | None = None
    pin: bool = False


class CheckpointResponse(BaseModel):
    checkpoint_id: str
    recipe_id: str | None = None


@router.post("/{computer_id}/checkpoint", response_model=CheckpointResponse)
async def checkpoint_computer(
    computer_id: str,
    request: Request,
    body: CheckpointRequest | None = None,
    account: Account = _require_account,
) -> CheckpointResponse:
    import uuid
    from datetime import UTC, datetime

    rt = get_runtime(request)
    db = rt.db
    config = rt.config
    vm_mgr = rt.vm_manager
    computer = await _get_running_computer(db, computer_id, account)

    checkpoint_id = f"ckpt-{uuid.uuid4().hex[:12]}"
    snapshot_dir = config.checkpoint_local_dir / checkpoint_id

    async with timed("checkpoint"):
        # Flush guest filesystem buffers to the block device so the disk
        # snapshot captures all written data (guest page cache is not visible
        # to dm-thin snapshots).
        await rt.host.guest.exec(computer.vm_ip, "sync", timeout=10.0)

        # Pause/snapshot/resume (sub-1s for the agent)
        await rt.host.hypervisor.snapshot(computer.socket_path, snapshot_dir)

        # Evict SSH pool connection — pause/resume disrupts the TCP session
        await rt.host.guest.evict(computer.vm_ip)

        # Freeze disk state: create a dm-thin CoW snapshot so fork gets the disk
        # as it was at checkpoint time, not the computer's evolving state.
        ckpt_volume_id = await vm_mgr.snapshot_disk_for_checkpoint(
            computer,
            checkpoint_id,
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
            r2_prefix=r2_prefix,
            disk_delta_size_bytes=None,
            memory_size_bytes=None,
            label=body.label if body else None,
            pinned=body.pin if body else False,
            created_at=now,
            recipe_id=computer.recipe_id,
        )
        await insert_checkpoint(db, ckpt)
    checkpoints_total.labels(trigger="api").inc()

    # Async background upload to R2
    rt.tasks.spawn(
        rt.host.objects.upload_dir(snapshot_dir, r2_prefix),
        name=f"upload:{checkpoint_id}",
        key=f"upload:{checkpoint_id}",
    )

    return CheckpointResponse(
        checkpoint_id=checkpoint_id,
        recipe_id=computer.recipe_id,
    )


async def _process_deferred(
    label: str,
    deferred_items: list[DeferredRequest],
    db: aiosqlite.Connection,
    config: Config,
    vm_mgr: VMManager,
    account: Account,
    host: Host,
    tasks: BackgroundTasks,
) -> None:
    """Boot a new computer from the latest checkpoint for a label and process deferred forks.

    Writes each deferred exec to /tmp/exec/0.txt, /tmp/exec/1.txt, etc.
    If meta_exec is set on any deferred request, uses that as the exec command.
    Otherwise, concatenates all exec commands with newlines.
    """
    import json

    from mshkn.db import list_checkpoints_by_account

    try:
        # Find the latest checkpoint with this label
        all_ckpts = await list_checkpoints_by_account(db, account.id, label=label)
        if not all_ckpts:
            logger.warning("No checkpoints found with label %s for deferred processing", label)
            return

        latest_ckpt = all_ckpts[0]  # sorted by created_at DESC

        # Parse all deferred payloads
        payloads = [json.loads(d.request_payload) for d in deferred_items]

        # Fork from latest checkpoint
        computer = await vm_mgr.fork_from_checkpoint(
            account.id,
            latest_ckpt,
            recipe_id=latest_ckpt.recipe_id,
        )

        # Write each deferred exec to /tmp/exec/N.txt
        exec_commands = [p.get("exec", "") or "" for p in payloads]
        mkdir_cmd = "mkdir -p /tmp/exec"
        write_cmds = [mkdir_cmd]
        for i, cmd in enumerate(exec_commands):
            # Escape single quotes for shell
            escaped = cmd.replace("'", "'\\''")
            write_cmds.append(f"printf '%s' '{escaped}' > /tmp/exec/{i}.txt")
        await host.guest.exec(computer.vm_ip, " && ".join(write_cmds))

        # Determine exec command: meta_exec from last request, or concatenate
        meta_exec = None
        for p in reversed(payloads):
            if p.get("meta_exec"):
                meta_exec = p["meta_exec"]
                break

        exec_cmd = meta_exec or "\n".join(c for c in exec_commands if c)

        # Run the exec
        if exec_cmd:
            result = await host.guest.exec(computer.vm_ip, exec_cmd)
            logger.info(
                "Deferred exec for label %s: exit_code=%d",
                label,
                result.exit_code,
            )

            # Self-destruct if any deferred request wanted it
            should_self_destruct = any(p.get("self_destruct") for p in payloads)
            last_callback = None
            for p in reversed(payloads):
                if p.get("callback_url"):
                    last_callback = p["callback_url"]
                    break

            if should_self_destruct:
                await _self_destruct(
                    computer=computer,
                    account=account,
                    label=label,
                    source_checkpoint_id=latest_ckpt.id,
                    exec_exit_code=result.exit_code,
                    exec_stdout=result.stdout,
                    exec_stderr=result.stderr,
                    callback_url=last_callback,
                    db=db,
                    config=config,
                    vm_mgr=vm_mgr,
                    host=host,
                    tasks=tasks,
                )

        logger.info(
            "Processed %d deferred request(s) for label %s -> computer %s",
            len(deferred_items),
            label,
            computer.id,
        )
    except Exception:
        logger.exception("Failed to process deferred queue for label %s", label)


@router.delete("/{computer_id}")
async def destroy_computer(
    request: Request,
    computer_id: str,
    account: Account = _require_account,
) -> dict[str, str]:
    rt = get_runtime(request)
    db = rt.db
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id:
        raise HTTPException(status_code=404, detail="Computer not found")
    if computer.status == ComputerStatus.DESTROYED:
        raise HTTPException(status_code=404, detail="Computer not found")
    vm_mgr = rt.vm_manager
    async with timed("destroy"):
        await vm_mgr.destroy(computer_id)
    computers_active.dec()

    # Drain deferred queue: if this computer was serving a labeled checkpoint chain,
    # process any queued fork requests.
    config = rt.config
    if computer.source_checkpoint_id:
        source_ckpt = await get_checkpoint(db, computer.source_checkpoint_id)
        if source_ckpt and source_ckpt.label:
            deferred = await claim_deferred_by_label(db, source_ckpt.label)
            if deferred:
                # Process deferred requests in background
                rt.tasks.spawn(
                    _process_deferred(
                        label=source_ckpt.label,
                        deferred_items=deferred,
                        db=db,
                        config=config,
                        vm_mgr=vm_mgr,
                        account=account,
                        host=rt.host,
                        tasks=rt.tasks,
                    ),
                    name=f"deferred:{source_ckpt.label}",
                )

    return {"status": ComputerStatus.DESTROYED}
