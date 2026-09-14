from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from membrane.postconditions import CHECKS

from mshkn.app import create_app
from mshkn.config import Config
from mshkn.db import connect, run_migrations
from mshkn.host.fake import FakeHost
from mshkn.runtime import Runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import aiosqlite
    import httpx
    from fastapi import FastAPI

    from mshkn.host import Host


@pytest.fixture(autouse=True)
def _checks_registry() -> Iterator[None]:
    """A capability's module registers what only it needs into the global
    `CHECKS` on import (capabilities design §4), and never unregisters it: a
    test that loads such a module leaves its checks behind for the next one,
    which is what `test_embryo_postconditions.py`'s `set(CHECKS) - INVARIANTS
    == EXERCISES` needs to not see. Snapshotting and restoring here replaces
    the hand-written `CHECKS.pop(...)` cleanup that used to live in every test
    module that loads one."""
    before = dict(CHECKS)
    try:
        yield
    finally:
        CHECKS.clear()
        CHECKS.update(before)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(tmp_path / "test.db")
    await run_migrations(conn, Path("migrations"))
    try:
        yield conn
    finally:
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
    """A Runtime for API tests: real DB and services, in-memory Host, no reaper loop."""
    return Runtime.build(config, db, host if host is not None else FakeHost(), http=http)


def make_app(runtime: Runtime) -> FastAPI:
    return create_app(runtime)


@pytest.fixture
async def runtime(db: aiosqlite.Connection, runtime_config: Config) -> AsyncIterator[Runtime]:
    """A Runtime on a FakeHost whose http client, tasks and volumes go on teardown."""
    host = FakeHost()
    rt = make_runtime(db, config=runtime_config, host=host)
    try:
        yield rt
    finally:
        await rt.tasks.drain(timeout=2.0)
        await rt.http.aclose()
        host.close()
