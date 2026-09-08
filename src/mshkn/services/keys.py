"""Scoped keys (#88): create, list and delete the second kind of credential."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mshkn.db import delete_api_key, get_api_key, insert_api_key, list_api_keys_by_account
from mshkn.errors import NotFound
from mshkn.models import ApiKey

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.models import Account, Scopes


class KeyService:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def create(self, account: Account, scopes: Scopes, label: str | None) -> ApiKey:
        key = ApiKey(
            id=f"key-{uuid.uuid4().hex[:12]}",
            account_id=account.id,
            secret=f"mk-{secrets.token_urlsafe(32)}",
            scopes=scopes,
            label=label,
            created_at=datetime.now(UTC).isoformat(),
        )
        await insert_api_key(self.db, key)
        return key

    async def list(self, account: Account) -> list[ApiKey]:
        return await list_api_keys_by_account(self.db, account.id)

    async def delete(self, account: Account, key_id: str) -> None:
        key = await get_api_key(self.db, key_id)
        if key is None or key.account_id != account.id:
            raise NotFound("Key not found")
        await delete_api_key(self.db, key_id)
