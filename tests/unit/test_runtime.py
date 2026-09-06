from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mshkn.app import create_app
from mshkn.db import insert_account, insert_computer
from mshkn.host.fake import FakeHost
from mshkn.runtime import BackgroundTasks, Runtime
from tests.support import account_row, computer_row
from tests.unit.conftest import make_runtime

if TYPE_CHECKING:
    import aiosqlite

    from mshkn.config import Config


async def _sleep_then(result: list[str], tag: str, seconds: float) -> None:
    await asyncio.sleep(seconds)
    result.append(tag)


async def test_spawn_tracks_and_forgets_tasks() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "a", 0.01), name="a")
    assert len(tasks) == 1
    await asyncio.sleep(0.05)
    assert out == ["a"]
    assert len(tasks) == 0


async def test_cancel_by_key_stops_the_task() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "slow", 10), name="slow", key="upload:ckpt-1")
    await tasks.cancel("upload:ckpt-1")
    assert out == []
    assert len(tasks) == 0


async def test_wait_by_key_returns_after_completion() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "x", 0.01), name="x", key="k")
    await tasks.wait("k")
    assert out == ["x"]
    await tasks.wait("missing")  # no-op


async def test_drain_waits_then_cancels_stragglers() -> None:
    tasks = BackgroundTasks()
    out: list[str] = []
    tasks.spawn(_sleep_then(out, "fast", 0.01), name="fast")
    tasks.spawn(_sleep_then(out, "slow", 10), name="slow")
    await tasks.drain(timeout=0.1)
    assert out == ["fast"]
    assert len(tasks) == 0


async def test_drain_awaits_tasks_spawned_while_draining() -> None:
    """A deferred drain spawns more work as it runs; drain must follow it."""
    tasks = BackgroundTasks()
    out: list[str] = []

    async def parent() -> None:
        await asyncio.sleep(0.01)
        out.append("parent")
        tasks.spawn(_sleep_then(out, "child", 0.01), name="child")

    tasks.spawn(parent(), name="parent")
    await tasks.drain(timeout=2.0)
    assert out == ["parent", "child"]
    assert len(tasks) == 0


async def test_failed_task_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    tasks = BackgroundTasks()
    tasks.spawn(boom(), name="boom")
    await asyncio.sleep(0.01)
    assert len(tasks) == 0
    assert any("boom" in r.getMessage() for r in caplog.records)


async def test_lifespan_closes_runtime_even_when_start_fails(
    db: aiosqlite.Connection,
    runtime_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing start() (e.g. dm-thin pool missing) must not leak the db connection."""
    host = FakeHost()

    async def boom() -> int | None:
        raise RuntimeError("pool missing")

    monkeypatch.setattr(host.blocks, "max_volume_id", boom)
    rt = make_runtime(db, config=runtime_config, host=host)
    spy = AsyncMock(wraps=rt.db.close)
    monkeypatch.setattr(rt.db, "close", spy)

    app = create_app(rt)

    with pytest.raises(RuntimeError, match="pool missing"):
        async with app.router.lifespan_context(app):
            pass

    spy.assert_awaited_once()


async def test_from_env_builds_a_runtime_on_the_configured_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mshkn.runtime as runtime_module

    monkeypatch.setenv("MSHKN_DB_PATH", str(tmp_path / "env.db"))
    monkeypatch.setenv("MSHKN_MIGRATIONS_DIR", str(Path("migrations").resolve()))
    monkeypatch.setattr(runtime_module, "firecracker_host", lambda _config: FakeHost())
    rt = await Runtime.from_env()
    try:
        cursor = await rt.db.execute("SELECT COUNT(*) FROM _migrations")
        row = await cursor.fetchone()
        assert row is not None and row[0] > 0
        assert rt.config.db_path == tmp_path / "env.db"
    finally:
        await rt.close()


async def test_start_spawns_the_reaper_and_close_tears_everything_down(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    async def record(name: str) -> None:
        closed.append(name)

    monkeypatch.setattr(runtime.host.guest, "close", lambda: record("guest"))
    monkeypatch.setattr(runtime.host.proxy, "close", lambda: record("proxy"))
    original_db_close = runtime.db.close

    async def db_close() -> None:
        closed.append("db")
        await original_db_close()

    monkeypatch.setattr(runtime.db, "close", db_close)

    await runtime.start()
    assert "reaper" in {t.get_name() for t in runtime.tasks._tasks}

    await runtime.close()
    assert closed == ["guest", "proxy", "db"]
    assert len(runtime.tasks) == 0
    assert runtime.http.is_closed


async def test_start_reaps_a_vm_whose_process_died_while_the_server_was_down(
    runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mshkn.runtime")
    await insert_account(runtime.db, account_row())
    await insert_computer(runtime.db, computer_row(1))  # PID 1001, never booted here

    await runtime.start()
    try:
        assert any("Startup: reaped 1 dead VM(s)" in r.getMessage() for r in caplog.records)
    finally:
        await runtime.close()
