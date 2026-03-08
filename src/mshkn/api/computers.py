from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mshkn.api.auth import require_account

if TYPE_CHECKING:
    from mshkn.models import Account

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
    body: CreateRequest,
    account: Account = _require_account,
) -> CreateResponse:
    raise NotImplementedError


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
    computer_id: str,
    account: Account = _require_account,
) -> dict[str, str]:
    raise NotImplementedError
