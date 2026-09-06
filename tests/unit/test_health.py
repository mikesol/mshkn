from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config


async def test_health(db: aiosqlite.Connection, runtime_config: Config) -> None:
    app = make_app(make_runtime(db, config=runtime_config))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
