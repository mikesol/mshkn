from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.runtime import BackgroundTasks, Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite
    from fastapi import FastAPI

    from mshkn.host import Host

# Everything a test built that has to be shut down, in the order it was built.
# A helper that made a BackgroundTasks or an httpx client by hand left its
# spawned work running and its transport open past the end of the test (#69);
# the `db` fixture empties this on the way out, before the connection closes,
# because draining is what flushes a task's last write.
_OWNED: list[BackgroundTasks | httpx.AsyncClient] = []


def owned_tasks() -> BackgroundTasks:
    """A BackgroundTasks the harness drains when the test ends."""
    tasks = BackgroundTasks()
    _OWNED.append(tasks)
    return tasks


def owned_client(**kwargs: Any) -> httpx.AsyncClient:
    """An httpx.AsyncClient the harness closes when the test ends."""
    client = httpx.AsyncClient(**kwargs)
    _OWNED.append(client)
    return client


async def _release_owned() -> None:
    owned, _OWNED[:] = list(_OWNED), []
    for item in owned:
        if isinstance(item, BackgroundTasks):
            await item.drain(timeout=2.0)
    for item in owned:
        if isinstance(item, httpx.AsyncClient):
            await item.aclose()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    _OWNED.clear()  # a test that failed in teardown must not bleed into the next
    conn = await connect(tmp_path / "test.db")
    await run_migrations(conn, Path("migrations"))
    try:
        yield conn
    finally:
        await _release_owned()
        await conn.close()


@pytest.fixture
def runtime_config(tmp_path: Path) -> Config:
    """A Config whose writable paths live under tmp_path (templates, checkpoints)."""
    return Config(
        domain="test.dev",
        checkpoint_local_dir=tmp_path / "ckpts",
        checkpoint_staging_dir=tmp_path / "staging",
    )


def make_runtime(
    db: aiosqlite.Connection,
    *,
    config: Config,
    host: Host | None = None,
    http: httpx.AsyncClient | None = None,
) -> Runtime:
    """A Runtime for API tests: real DB and services, in-memory Host, no reaper loop.

    Its tasks and its http client are the harness's to release, whether the
    client came from the caller or from `Runtime.build` itself.
    """
    rt = Runtime.build(config, db, host if host is not None else FakeHost(), http=http)
    _OWNED.extend((rt.tasks, rt.http))
    return rt


def make_app(runtime: Runtime) -> FastAPI:
    return create_app(runtime)


@pytest.fixture
async def runtime(db: aiosqlite.Connection, runtime_config: Config) -> AsyncIterator[Runtime]:
    """A Runtime on a FakeHost whose http client, tasks and volumes go on teardown."""
    host = FakeHost()
    try:
        yield make_runtime(db, config=runtime_config, host=host)
    finally:
        # Before the volumes go: a task still draining writes into them.
        await _release_owned()
        host.close()
