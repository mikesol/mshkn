"""The unauthenticated system endpoints: health subsystems and the alert history
the reaper's host check feeds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.api import system as system_module
from mshkn.host import PoolUsage

if TYPE_CHECKING:
    import pytest

    from mshkn.config import Config

    from .conftest import Flow


async def test_health_subsystems_and_alerts(flow: Flow, monkeypatch: pytest.MonkeyPatch) -> None:
    def present(config: Config) -> str:
        return "ok"

    monkeypatch.setattr(system_module, "_firecracker_present", present)
    body = (await flow.client.get("/health")).json()
    assert body["status"] == "ok" and set(body["subsystems"]) == {
        "database",
        "firecracker",
        "storage",
        "proxy",
    }
    flow.host.blocks.pool_usage = PoolUsage(data_used_ratio=0.9, metadata_used_ratio=0.1)
    await flow.runtime.reaper.check_host()
    alerts = (await flow.client.get("/alerts")).json()
    assert any(a["source"] == "thin_pool_data" and a["level"] == "warning" for a in alerts)
