from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.ratelimit import RateLimiter
from mshkn.runtime import BackgroundTasks, Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite
    from fastapi import FastAPI


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(tmp_path / "test.db")
    await run_migrations(conn, Path("migrations"))
    try:
        yield conn
    finally:
        await conn.close()


def make_runtime(
    db: aiosqlite.Connection,
    *,
    vm_manager: Any = None,
    config: Config | None = None,
    ssh_pool: Any = None,
) -> Runtime:
    """A Runtime for API tests: real DB, mocked VMManager, no Caddy, no reaper."""
    return Runtime(
        config=config if config is not None else Config(domain="test.dev"),
        db=db,
        vm_manager=vm_manager if vm_manager is not None else AsyncMock(),
        caddy=None,
        ssh_pool=ssh_pool,
        tasks=BackgroundTasks(),
        rate_limiter=RateLimiter(max_requests=80, window_seconds=10.0),
    )


def make_app(runtime: Runtime) -> FastAPI:
    return create_app(runtime)
