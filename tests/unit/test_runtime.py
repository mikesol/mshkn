from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mshkn.app import create_app
from mshkn.host.fake import FakeHost
from mshkn.runtime import BackgroundTasks
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
