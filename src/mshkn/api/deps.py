"""FastAPI dependencies: the Runtime and the authenticated principal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

from mshkn.db import get_account_by_id, get_account_by_key, get_api_key_by_secret
from mshkn.errors import Forbidden
from mshkn.models import Principal

if TYPE_CHECKING:
    from mshkn.models import Account
    from mshkn.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


async def require_principal(request: Request) -> Principal:
    """The bearer as a Principal: the account key first, then a scoped key (#88)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    db = get_runtime(request).db
    secret = auth[7:]
    account = await get_account_by_key(db, secret)
    if account is not None:
        return Principal(account=account)
    key = await get_api_key_by_secret(db, secret)
    if key is not None:
        owner = await get_account_by_id(db, key.account_id)
        if owner is not None:
            return Principal(account=owner, key=key)
    raise HTTPException(status_code=401, detail="Invalid API key")


async def require_account_key(principal: Principal = Depends(require_principal)) -> Account:  # noqa: B008
    """The account for routes a scoped key may never call (#88)."""
    if principal.key is not None:
        raise Forbidden("This route requires the account key")
    return principal.account
