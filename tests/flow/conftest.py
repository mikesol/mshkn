"""Flow tier: the real app and services against the in-memory fake host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, insert_account, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.models import Account
from mshkn.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from mshkn.host.fake import FakeHostInstance
    from mshkn.ratelimit import RateLimiter

AUTH = {"Authorization": "Bearer test-key"}
OTHER_AUTH = {"Authorization": "Bearer other-key"}


@dataclass
class Flow:
    app: FastAPI
    runtime: Runtime
    host: FakeHostInstance
    client: AsyncClient
    other_client: AsyncClient
    received: list[dict[str, Any]]


def _receiver(received: list[dict[str, Any]]) -> FastAPI:
    """The callback target: POST /cb records the JSON body it was sent."""
    app = FastAPI()

    @app.post("/cb")
    async def receive(payload: dict[str, Any]) -> dict[str, str]:
        received.append(payload)
        return {"status": "ok"}

    return app


@asynccontextmanager
async def _build_flow(config: Config, tmp_path: Path) -> AsyncIterator[Flow]:
    """The shared body of both fixtures: a migrated database, a fake host, a
    Runtime whose callback client posts into an in-process receiver, and the
    real ASGI app in front of it."""
    config.ssh_key_path.parent.mkdir(parents=True, exist_ok=True)
    config.ssh_key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAAflowtest mshkn@flow\n")
    db = await connect(tmp_path / "flow.db")
    await run_migrations(db, Path("migrations"))
    await insert_account(
        db,
        Account(id="acct-1", api_key="test-key", vm_limit=10, created_at="2026-09-05T00:00:00"),
    )
    await insert_account(
        db,
        Account(id="acct-2", api_key="other-key", vm_limit=10, created_at="2026-09-05T00:00:00"),
    )
    host = FakeHost()
    received: list[dict[str, Any]] = []
    callbacks = AsyncClient(
        transport=ASGITransport(app=_receiver(received)), base_url="http://receiver"
    )
    runtime = Runtime.build(config, db, host, http=callbacks)
    # start() would also spawn the reaper loop; the flow tier drives everything
    # explicitly, so only the allocator's startup recovery runs here.
    await runtime.allocator.initialize(db, host.blocks)
    app = create_app(runtime)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://flow", headers=AUTH) as client,
        AsyncClient(
            transport=transport, base_url="http://flow", headers=OTHER_AUTH
        ) as other_client,
    ):
        try:
            yield Flow(
                app=app,
                runtime=runtime,
                host=host,
                client=client,
                other_client=other_client,
                received=received,
            )
        finally:
            await runtime.tasks.drain(timeout=2.0)
            await runtime.http.aclose()
            await db.close()


@pytest.fixture
async def flow(tmp_path: Path) -> AsyncIterator[Flow]:
    config = Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "checkpoints",
        idle_timeout_seconds=0,
        ssh_key_path=tmp_path / "id_ed25519",
    )
    async with _build_flow(config, tmp_path) as built:
        yield built


@pytest.fixture
def flow_factory(tmp_path: Path) -> Callable[..., AbstractAsyncContextManager[Flow]]:
    """Build a Flow with Config overrides (idle_timeout_seconds, checkpoint_retention_count)."""

    @asynccontextmanager
    async def make(**overrides: Any) -> AsyncIterator[Flow]:
        rate_limit: RateLimiter | None = overrides.pop("rate_limit", None)
        config = Config(
            domain="test.dev",
            checkpoint_local_dir=tmp_path / "checkpoints",
            idle_timeout_seconds=0,
            ssh_key_path=tmp_path / "id_ed25519",
        )
        config = replace(config, **overrides)
        async with _build_flow(config, tmp_path) as built:
            if rate_limit is not None:
                built.runtime.rate_limiter = rate_limit
            yield built

    return make
