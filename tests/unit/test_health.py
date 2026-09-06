from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

import mshkn.api.system as system_module
from mshkn.host.fake import FakeHost
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite
    import pytest

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
    db: aiosqlite.Connection, runtime_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_module, "_firecracker_present", lambda _config: "ok")
    body = await _health(db, runtime_config, FakeHost())
    assert body == {
        "status": "ok",
        "subsystems": {"database": "ok", "firecracker": "ok", "storage": "ok", "proxy": "ok"},
    }


async def test_health_is_degraded_but_200_when_a_subsystem_fails(
    db: aiosqlite.Connection, runtime_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system_module, "_firecracker_present", lambda _config: "ok")
    host = FakeHost()
    host.proxy.is_healthy = False
    body = await _health(db, runtime_config, host)
    assert body["status"] == "degraded"
    subsystems = body["subsystems"]
    assert isinstance(subsystems, dict)
    assert subsystems["proxy"] != "ok"
    assert subsystems["database"] == "ok"


async def test_health_reports_a_missing_firecracker_binary(
    db: aiosqlite.Connection, runtime_config: Config
) -> None:
    body = await _health(db, runtime_config, FakeHost())
    subsystems = body["subsystems"]
    assert isinstance(subsystems, dict)
    assert body["status"] == "degraded" and "firecracker" in str(subsystems["firecracker"])
