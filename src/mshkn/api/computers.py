from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from mshkn.api.auth import require_account
from mshkn.models import Manifest

if TYPE_CHECKING:
    from mshkn.config import Config
    from mshkn.models import Account
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


class ExecRequest(BaseModel):
    command: str


@router.post("/{computer_id}/exec")
async def exec_command(
    computer_id: str,
    body: ExecRequest,
    account: Account = _require_account,
) -> dict[str, object]:
    raise NotImplementedError


@router.delete("/{computer_id}")
async def destroy_computer(
    request: Request,
    computer_id: str,
    account: Account = _require_account,  # noqa: ARG001
) -> dict[str, str]:
    vm_mgr: VMManager = request.app.state.vm_manager
    await vm_mgr.destroy(computer_id)
    return {"status": "destroyed"}
