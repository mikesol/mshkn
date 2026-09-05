"""FastAPI dependencies: the Runtime and the authenticated account."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from mshkn.db import get_account_by_key

if TYPE_CHECKING:
    from mshkn.models import Account
    from mshkn.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


async def require_account(request: Request) -> Account:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    account = await get_account_by_key(get_runtime(request).db, auth[7:])
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return account
