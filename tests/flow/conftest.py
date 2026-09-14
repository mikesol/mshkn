"""Flow tier: the real app and services against the in-memory fake host."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, insert_account, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.models import Account
from mshkn.runtime import Runtime
from tests.support import present_firecracker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from mshkn.host.fake import FakeHostInstance
    from mshkn.ratelimit import RateLimiter

AUTH = {"Authorization": "Bearer test-key"}
OTHER_AUTH = {"Authorization": "Bearer other-key"}


class HostRouter(httpx.AsyncBaseTransport):
    """One in-process transport per hostname: the callback receiver, and whatever
    a test mounts (a relay target, the scripted model server)."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self.routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self.routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no route to {request.url.host}")
        return await transport.handle_async_request(request)


async def _public_resolver(hostname: str) -> list[str]:
    """Every in-process host resolves to a public address, so the guard lets it through."""
    return ["93.184.216.34"]


async def _no_sleep(seconds: float) -> None:
    return None


@dataclass
class Flow:
    app: FastAPI
    runtime: Runtime
    host: FakeHostInstance
    client: AsyncClient
    other_client: AsyncClient
    received: list[dict[str, Any]]
    targets: dict[str, httpx.AsyncBaseTransport]


def _receiver(received: list[dict[str, Any]]) -> FastAPI:
    """The callback target: POST /cb records the JSON body it was sent."""
    app = FastAPI()

    @app.post("/cb")
    async def receive(payload: dict[str, Any]) -> dict[str, str]:
        received.append(payload)
        return {"status": "ok"}

    return app


@asynccontextmanager
async def _build_flow(config: Config, home: Path) -> AsyncIterator[Flow]:
    """The shared body of both fixtures: a migrated database, a fake host, a
    Runtime whose callback client posts into an in-process receiver, and the
    real ASGI app in front of it. `home` is this flow's alone — two flows open
    at once must not share a database file."""
    home.mkdir(parents=True, exist_ok=True)
    config.ssh_key_path.parent.mkdir(parents=True, exist_ok=True)
    config.ssh_key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAAflowtest mshkn@flow\n")
    db = await connect(home / "flow.db")
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
    targets: dict[str, httpx.AsyncBaseTransport] = {
        "receiver": ASGITransport(app=_receiver(received))
    }
    callbacks = AsyncClient(transport=HostRouter(targets), base_url="http://receiver")
    runtime = Runtime.build(config, db, host, http=callbacks)
    runtime.relay.resolve = _public_resolver
    runtime.relay.sleep = _no_sleep
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
                targets=targets,
            )
        finally:
            await runtime.tasks.drain(timeout=2.0)
            await runtime.http.aclose()
            await db.close()
            host.close()


def _config(home: Path) -> Config:
    home.mkdir(parents=True, exist_ok=True)
    return Config(
        domain="test.dev",
        checkpoint_local_dir=home / "checkpoints",
        checkpoint_staging_dir=home / "staging",
        idle_timeout_seconds=0,
        ssh_key_path=home / "id_ed25519",
        **present_firecracker(home),  # type: ignore[arg-type]
    )


@pytest.fixture
async def flow(tmp_path: Path) -> AsyncIterator[Flow]:
    home = tmp_path / "flow-0"
    async with _build_flow(_config(home), home) as built:
        yield built


@pytest.fixture
def flow_factory(tmp_path: Path) -> Callable[..., AbstractAsyncContextManager[Flow]]:
    """Build a Flow with Config overrides (idle_timeout_seconds, checkpoint_retention_count)."""

    homes = count()

    @asynccontextmanager
    async def make(**overrides: Any) -> AsyncIterator[Flow]:
        rate_limit: RateLimiter | None = overrides.pop("rate_limit", None)
        home = tmp_path / f"flow-{next(homes)}"
        config = replace(_config(home), **overrides)
        async with _build_flow(config, home) as built:
            if rate_limit is not None:
                built.runtime.rate_limiter = rate_limit
            yield built

    return make
