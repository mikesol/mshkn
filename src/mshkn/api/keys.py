"""Scoped-key management (#88). Account key only: a scoped key gets 403 here."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request

from mshkn.api.deps import get_runtime, require_account_key
from mshkn.api.schemas import CreateKeyRequest, DeleteResponse, KeyCreatedResponse, KeyResponse
from mshkn.models import parse_scopes

if TYPE_CHECKING:
    from mshkn.models import Account

router = APIRouter(prefix="/keys", tags=["keys"])

_require_account_key = Depends(require_account_key)


@router.post("", response_model=KeyCreatedResponse)
async def create_key(
    request: Request,
    body: CreateKeyRequest,
    account: Account = _require_account_key,
) -> KeyCreatedResponse:
    rt = get_runtime(request)
    key = await rt.keys.create(account, parse_scopes(body.scopes), body.label)
    return KeyCreatedResponse(
        id=key.id,
        label=key.label,
        scopes=key.scopes.to_document(),
        created_at=key.created_at,
        secret=key.secret,
    )


@router.get("", response_model=list[KeyResponse])
async def list_keys(request: Request, account: Account = _require_account_key) -> list[KeyResponse]:
    rt = get_runtime(request)
    return [
        KeyResponse(id=k.id, label=k.label, scopes=k.scopes.to_document(), created_at=k.created_at)
        for k in await rt.keys.list(account)
    ]


@router.delete("/{key_id}", response_model=DeleteResponse)
async def delete_key(
    key_id: str, request: Request, account: Account = _require_account_key
) -> DeleteResponse:
    await get_runtime(request).keys.delete(account, key_id)
    return DeleteResponse(status="deleted")
