from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from mshkn.observability.metrics import checkpoints_total, computers_created_total
from tests.unit.conftest import make_app, make_runtime

if TYPE_CHECKING:
    import aiosqlite


async def test_metrics_endpoint_returns_200(db: aiosqlite.Connection) -> None:
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")


async def test_metrics_contains_expected_names(db: aiosqlite.Connection) -> None:
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/metrics")
    text = resp.text
    assert "mshkn_computers_active" in text
    assert "mshkn_computers_created_total" in text
    assert "mshkn_checkpoints_total" in text
    assert "mshkn_exec_duration_seconds" in text
    assert "mshkn_operation_duration_seconds" in text
    assert "mshkn_operation_errors_total" in text
    assert "mshkn_thin_pool_used_ratio" in text
    assert "mshkn_host_ram_used_ratio" in text
    assert "# HELP" in text
    assert "# TYPE" in text


async def test_labelled_counters_render_after_first_increment(db: aiosqlite.Connection) -> None:
    checkpoints_total.labels(trigger="api").inc()
    computers_created_total.labels(source="fork").inc()
    app = make_app(make_runtime(db))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        text = (await client.get("/metrics")).text
    assert 'mshkn_checkpoints_total{trigger="api"}' in text
    assert 'mshkn_computers_created_total{source="fork"}' in text
