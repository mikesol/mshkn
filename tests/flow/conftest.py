"""Flow tier: the real app and services against the in-memory fake host."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, insert_account, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.models import Account
from mshkn.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from mshkn.host.fake import FakeHostInstance

AUTH = {"Authorization": "Bearer test-key"}


@dataclass
class Flow:
    app: FastAPI
    runtime: Runtime
    host: FakeHostInstance
    client: AsyncClient


@pytest.fixture
async def flow(tmp_path: Path) -> AsyncIterator[Flow]:
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "checkpoints",
        idle_timeout_seconds=0,
    )
    db = await connect(tmp_path / "flow.db")
    await run_migrations(db, Path("migrations"))
    await insert_account(
        db,
        Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-09-05T00:00:00"),
    )
    host = FakeHost()
    runtime = Runtime.build(config, db, host)
    # start() would also spawn the reaper loop; the flow tier drives everything
    # explicitly, so only the allocator's startup recovery runs here.
    await runtime.allocator.initialize(db, host.blocks)
    app = create_app(runtime)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://flow", headers=AUTH) as client:
        try:
            yield Flow(app=app, runtime=runtime, host=host, client=client)
        finally:
            await runtime.tasks.drain(timeout=2.0)
            await runtime.http.aclose()
            await db.close()
