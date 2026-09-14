from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.host.fake import FakeHost
from tests.support import present_firecracker
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

    from mshkn.config import Config
    from mshkn.host.fake import FakeHostInstance


async def _health(
    db: aiosqlite.Connection, config: Config, host: FakeHostInstance
) -> dict[str, object]:
    app = make_app(make_runtime(db, config=config, host=host))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body: dict[str, object] = resp.json()
    return body


async def test_health_is_ok_when_every_subsystem_answers(
    db: aiosqlite.Connection, runtime_config: Config, tmp_path: Path
) -> None:
    config = replace(runtime_config, **present_firecracker(tmp_path))  # type: ignore[arg-type]
    body = await _health(db, config, FakeHost())
    assert body == {
        "status": "ok",
        "subsystems": {"database": "ok", "firecracker": "ok", "storage": "ok", "proxy": "ok"},
    }


async def test_health_is_degraded_but_200_when_a_subsystem_fails(
    db: aiosqlite.Connection, runtime_config: Config, tmp_path: Path
) -> None:
    config = replace(runtime_config, **present_firecracker(tmp_path))  # type: ignore[arg-type]
    host = FakeHost()
    host.proxy.is_healthy = False
    body = await _health(db, config, host)
    assert body["status"] == "degraded"
    subsystems = body["subsystems"]
    assert isinstance(subsystems, dict)
    assert subsystems["proxy"] != "ok"
    assert subsystems["database"] == "ok"


async def test_health_reports_a_missing_firecracker_binary(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    """Named, not inherited from the box: on a host that does have firecracker
    on PATH this used to pass on the kernel check and assert nothing."""
    config = replace(runtime_config, firecracker_binary="mshkn-no-such-binary")
    body = await _health(db, config, FakeHost())
    subsystems = body["subsystems"]
    assert isinstance(subsystems, dict)
    assert body["status"] == "degraded"
    assert subsystems["firecracker"] == "firecracker binary mshkn-no-such-binary not on PATH"


async def test_health_reports_the_database_degraded_while_reaper_cycles_fail(
    db: aiosqlite.Connection, runtime_config: Config, tmp_path: Path
) -> None:
    """Reads worked throughout the #105 incident; the reaper's failing writes are the signal."""
    config = replace(runtime_config, **present_firecracker(tmp_path))  # type: ignore[arg-type]
    rt = make_runtime(db, config=config, host=FakeHost())
    rt.reaper.consecutive_failures = 3
    rt.reaper.last_failure = "OperationalError: database is locked"
    app = make_app(rt)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["subsystems"]["database"] == (
        "reaper: 3 consecutive cycle failures, last: OperationalError: database is locked"
    )
