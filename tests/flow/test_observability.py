"""Health, metrics and alerts as an operator sees them: a failing database degrades
health, /metrics renders every series the runtime owns, and /alerts is the reaper's
history verbatim."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.api import system as system_module
from mshkn.host import PoolUsage

if TYPE_CHECKING:
    import pytest

    from mshkn.config import Config

    from .conftest import Flow


async def test_health_degrades_when_the_database_fails_and_metrics_render(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    def present(config: Config) -> str:
        return "ok"

    # The one module seam the flow tier patches: the firecracker binary and the
    # kernel image are host facts, not runtime state the fake host can carry.
    monkeypatch.setattr(system_module, "_firecracker_present", present)
    assert (await flow.client.get("/health")).json()["status"] == "ok"

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(flow.runtime.db, "execute", broken)
    body = (await flow.client.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["subsystems"]["database"] == "RuntimeError: disk I/O error"
    assert body["subsystems"]["storage"] == "ok" and body["subsystems"]["proxy"] == "ok"
    monkeypatch.undo()

    text = (await flow.client.get("/metrics")).text
    for series in (
        "mshkn_operation_duration_seconds",
        "mshkn_operation_errors_total",
        "mshkn_computers_active",
        "mshkn_computers_created_total",
        "mshkn_checkpoints_total",
        "mshkn_thin_pool_used_ratio",
        "mshkn_host_ram_used_ratio",
    ):
        assert series in text
    assert "mshkn_exec_duration_seconds" not in text, "exec is timed under op=exec now"


async def test_alerts_endpoint_returns_the_runtime_deque(flow: Flow) -> None:
    flow.host.blocks.pool_usage = PoolUsage(data_used_ratio=0.99, metadata_used_ratio=0.1)
    await flow.runtime.reaper.check_host()
    alerts = (await flow.client.get("/alerts")).json()
    entry = next(a for a in alerts if a["source"] == "thin_pool_data")
    assert entry["level"] == "critical"
    assert set(entry) == {"level", "source", "message", "value", "threshold", "timestamp"}
    assert entry["value"] == 0.99 and entry["threshold"] == 0.8
    assert not [a for a in alerts if a["source"] == "thin_pool_metadata"], (
        "metadata at 10% is below the warning threshold"
    )
