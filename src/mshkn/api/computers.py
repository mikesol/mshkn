from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from mshkn.api.auth import require_account
from mshkn.db import get_computer
from mshkn.models import Manifest
from mshkn.vm.ssh import ssh_download, ssh_exec, ssh_exec_bg, ssh_exec_stream, ssh_upload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

    from mshkn.config import Config
    from mshkn.models import Account, Computer
    from mshkn.vm.manager import VMManager

router = APIRouter(prefix="/computers", tags=["computers"])

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
    config: Config = request.app.state.config
    vm_mgr: VMManager = request.app.state.vm_manager
    manifest = Manifest(uses=body.uses)
    computer = await vm_mgr.create(account.id, manifest)
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
    db: aiosqlite.Connection = request.app.state.db
    config: Config = request.app.state.config
    computer = await _get_running_computer(db, computer_id, account)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        async for stream, line in ssh_exec_stream(
            computer.vm_ip, body.command, config.ssh_key_path
        ):
            yield {"event": stream, "data": line}

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
        async for stream, line in ssh_exec_stream(
            computer.vm_ip,
            f"tail -f /tmp/bg-{pid}.log",
            config.ssh_key_path,
        ):
            yield {"event": stream, "data": line}

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
    computer = await get_computer(db, computer_id)
    if computer is None or computer.account_id != account.id:
        raise HTTPException(status_code=404, detail="Computer not found")
    return {
        "computer_id": computer.id,
        "status": computer.status,
        "vm_ip": computer.vm_ip,
        "manifest_hash": computer.manifest_hash,
        "created_at": computer.created_at,
        "last_exec_at": computer.last_exec_at,
    }


@router.delete("/{computer_id}")
async def destroy_computer(
    request: Request,
    computer_id: str,
    account: Account = _require_account,  # noqa: ARG001
) -> dict[str, str]:
    vm_mgr: VMManager = request.app.state.vm_manager
    await vm_mgr.destroy(computer_id)
    return {"status": "destroyed"}
