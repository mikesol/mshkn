"""The unauthenticated system endpoints as an operator sees them: /health names its
subsystems and degrades when the database fails, /metrics renders every series the
runtime owns, and /alerts is the reaper's history verbatim."""

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
    healthy = (await flow.client.get("/health")).json()
    assert healthy["status"] == "ok"
    assert set(healthy["subsystems"]) == {"database", "firecracker", "storage", "proxy"}

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
    for ratio in (0.9, 0.99):
        flow.host.blocks.pool_usage = PoolUsage(data_used_ratio=ratio, metadata_used_ratio=0.1)
        await flow.runtime.reaper.check_host()
    alerts = (await flow.client.get("/alerts")).json()
    data = [a for a in alerts if a["source"] == "thin_pool_data"]
    assert [a["level"] for a in data] == ["warning", "critical"], (
        "the deque keeps every check in the order the reaper ran them"
    )
    assert data[0]["value"] == 0.9 and data[1]["value"] == 0.99
    entry = data[1]
    assert set(entry) == {"level", "source", "message", "value", "threshold", "timestamp"}
    assert entry["threshold"] == 0.8 and "99.0%" in entry["message"]
    assert not [a for a in alerts if a["source"] == "thin_pool_metadata"], (
        "metadata at 10% is below the warning threshold"
    )
