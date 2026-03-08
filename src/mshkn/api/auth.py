from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from mshkn.db import get_account_by_key

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.models import Account


async def require_account(request: Request) -> Account:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    api_key = auth[7:]

    db: aiosqlite.Connection = request.app.state.db
    account = await get_account_by_key(db, api_key)
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return account
