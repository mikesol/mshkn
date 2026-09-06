"""Verify GET /computers/{id}/status doesn't hang when metrics gathering stalls.

If the reaper kills a VM between the DB read and the SSH connect for metrics,
the guest's metrics call can hang. ComputerService.metrics bounds that call with
STATUS_METRICS_TIMEOUT_SECONDS so the request still returns promptly with
metrics omitted.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

import mshkn.services.computers as computers_service
from mshkn.db import insert_account
from mshkn.host.fake import FakeHost
from mshkn.resources import DEFAULT_RESOURCES
from tests.support import account_row
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite
    import pytest

    from mshkn.config import Config
    from mshkn.host import VmMetrics

ACCOUNT = account_row(vm_limit=2)


async def test_status_endpoint_bounds_metrics_gather(
    db: aiosqlite.Connection,
    runtime_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await insert_account(db, ACCOUNT)
    host = FakeHost()
    rt = make_runtime(db, config=runtime_config, host=host)
    computer = await rt.computers.create(ACCOUNT, recipe_id=None, resources=DEFAULT_RESOURCES)

    monkeypatch.setattr(computers_service, "STATUS_METRICS_TIMEOUT_SECONDS", 0.05)

    async def hanging_metrics(vm_ip: str, *, timeout: float = 10.0) -> VmMetrics:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    monkeypatch.setattr(host.guest, "metrics", hanging_metrics)

    app = make_app(rt)
    transport = ASGITransport(app=app)

    start = time.monotonic()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/computers/{computer.id}/status",
            headers={"Authorization": "Bearer test-key"},
        )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert resp.status_code == 200
    body = resp.json()
    assert body["cpu_pct"] is None
    assert body["processes"] is None
    assert body["status"] == "running"
    assert body["computer_id"] == computer.id
    assert body["vm_ip"] == computer.vm_ip
