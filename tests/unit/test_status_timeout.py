"""Verify GET /computers/{id}/status doesn't hang when metrics gathering stalls.

If the reaper kills a VM between the DB read and the SSH connect for metrics,
ssh_gather_metrics can hang. The status endpoint bounds that call with
STATUS_METRICS_TIMEOUT_SECONDS so the request still returns promptly with
metrics omitted.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

import mshkn.api.computers as computers_module
from mshkn.db import insert_account, insert_computer
from mshkn.models import Account, ComputerStatus
from tests.unit.conftest import make_app, make_runtime
from tests.unit.test_vm_limit import _make_computer

if TYPE_CHECKING:
    import aiosqlite
    import pytest


async def _account(db: aiosqlite.Connection, vm_limit: int = 2) -> None:
    await insert_account(
        db,
        Account(
            id="acct-1",
            api_key="test-key",
            vm_limit=vm_limit,
            created_at="2026-03-08T00:00:00",
        ),
    )


async def test_status_endpoint_bounds_metrics_gather(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _account(db)
    await insert_computer(db, _make_computer(1, status=ComputerStatus.RUNNING))

    monkeypatch.setattr(computers_module, "STATUS_METRICS_TIMEOUT_SECONDS", 0.05)

    async def hanging_metrics(*a: object, **k: object) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(computers_module, "ssh_gather_metrics", hanging_metrics)

    app = make_app(make_runtime(db, vm_manager=AsyncMock()))
    transport = ASGITransport(app=app)

    start = time.monotonic()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/computers/comp-1/status",
            headers={"Authorization": "Bearer test-key"},
        )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert resp.status_code == 200
    body = resp.json()
    assert "cpu_pct" not in body
    assert body["status"] == "running"
