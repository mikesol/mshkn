"""The reaper through HTTP: idle checkpoints tagged trigger=idle, dead VMs cleaned
up, retention pruning that honours pins and cancels an in-flight upload."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mshkn.db import get_computer
from mshkn.host import ExecResult
from mshkn.observability.metrics import checkpoints_total

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import pytest

    from .conftest import Flow


async def test_idle_reap_checkpoints_with_trigger_idle_and_dead_reap_cleans_up(
    flow_factory: Callable[..., AbstractAsyncContextManager[Flow]],
) -> None:
    async with flow_factory(idle_timeout_seconds=60) as flow:
        host = flow.host
        host.guest.script["sync"] = ExecResult(0, "", "")
        cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
        stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        await flow.runtime.db.execute(
            "UPDATE computers SET created_at = ? WHERE id = ?", (stale, cid)
        )
        await flow.runtime.db.commit()
        before = checkpoints_total.labels(trigger="idle")._value.get()
        await flow.runtime.reaper.cycle()
        assert checkpoints_total.labels(trigger="idle")._value.get() == before + 1
        assert (await flow.client.get(f"/computers/{cid}/status")).status_code == 404
        listed = (
            await flow.client.get("/checkpoints", params={"label": "auto-idle-timeout"})
        ).json()
        assert len(listed) == 1
        dead = (await flow.client.post("/computers", json={})).json()["computer_id"]
        row = await get_computer(flow.runtime.db, dead)
        assert row is not None
        host.hypervisor.alive.pop(row.firecracker_pid or -1)
        await flow.runtime.reaper.cycle()
        assert (await flow.client.get(f"/computers/{dead}/status")).status_code == 404
        assert row.slot in flow.runtime.allocator.free_slots


async def test_prune_honours_retention_and_pin_and_cancels_uploads(
    flow_factory: Callable[..., AbstractAsyncContextManager[Flow]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with flow_factory(checkpoint_retention_count=1) as flow:
        host = flow.host
        host.guest.script["sync"] = ExecResult(0, "", "")
        cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
        pinned = (
            await flow.client.post(f"/computers/{cid}/checkpoint", json={"pin": True})
        ).json()["checkpoint_id"]
        old = (await flow.client.post(f"/computers/{cid}/checkpoint", json={})).json()[
            "checkpoint_id"
        ]
        gate = asyncio.Event()

        async def slow_upload(local_dir: Path, prefix: str) -> None:
            await gate.wait()
            assert local_dir.exists()

        monkeypatch.setattr(host.objects, "upload_dir", slow_upload)
        new = (await flow.client.post(f"/computers/{cid}/checkpoint", json={})).json()[
            "checkpoint_id"
        ]
        for ckpt_id, ts in (
            (pinned, "2026-01-01T00:00:00"),
            (old, "2026-01-02T00:00:00"),
            (new, "2026-01-03T00:00:00"),
        ):
            await flow.runtime.db.execute(
                "UPDATE checkpoints SET created_at = ? WHERE id = ?", (ts, ckpt_id)
            )
        await flow.runtime.db.commit()
        assert await flow.runtime.checkpoints.prune() == 1
        ids = {c["id"] for c in (await flow.client.get("/checkpoints")).json()}
        assert ids == {pinned, new}
        resp = await flow.client.delete(f"/checkpoints/{new}")
        assert resp.status_code == 200
        assert len(flow.runtime.tasks) == 0, "the in-flight upload was cancelled, not left to fail"
        gate.set()


async def test_idle_reap_preserves_the_source_label(
    flow_factory: Callable[..., AbstractAsyncContextManager[Flow]],
) -> None:
    async with flow_factory(idle_timeout_seconds=60) as flow:
        host = flow.host
        host.guest.script["sync"] = ExecResult(0, "", "")
        base = (await flow.client.post("/computers", json={})).json()["computer_id"]
        ckpt = (
            await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "keep"})
        ).json()["checkpoint_id"]
        await flow.client.delete(f"/computers/{base}")
        fork = (await flow.client.post(f"/checkpoints/{ckpt}/fork", json={})).json()["computer_id"]
        stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        await flow.runtime.db.execute(
            "UPDATE computers SET created_at = ? WHERE id = ?", (stale, fork)
        )
        await flow.runtime.db.commit()
        await flow.runtime.reaper.cycle()
        chain = (await flow.client.get("/checkpoints", params={"label": "keep"})).json()
        assert len(chain) == 2 and {c["parent_id"] for c in chain} == {None, ckpt}
        idle = await flow.client.get("/checkpoints", params={"label": "auto-idle-timeout"})
        assert idle.json() == [], "the fork kept its source label instead of the idle one"
