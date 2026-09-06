"""The deferred queue's drain: a destroy and an idle reap racing on one label
claim the queue once between them, so exactly one fork runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from .conftest import Flow


async def test_destroy_and_idle_reap_racing_on_one_label_fork_exactly_once(
    flow_factory: Callable[..., AbstractAsyncContextManager[Flow]],
) -> None:
    async with flow_factory(idle_timeout_seconds=60) as flow:
        host = flow.host
        host.guest.script["sync"] = ExecResult(0, "", "")
        host.guest.script["echo q"] = ExecResult(0, "q\n", "")
        base = (await flow.client.post("/computers", json={})).json()["computer_id"]
        ckpt = (
            await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "race"})
        ).json()["checkpoint_id"]
        await flow.client.delete(f"/computers/{base}")
        active = (
            await flow.client.post(
                f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"}
            )
        ).json()["computer_id"]
        queued = await flow.client.post(
            f"/checkpoints/{ckpt}/fork",
            json={"exclusive": "defer_on_conflict", "exec": "echo q"},
        )
        assert queued.status_code == 202
        stale = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        await flow.runtime.db.execute(
            "UPDATE computers SET created_at = ? WHERE id = ?", (stale, active)
        )
        await flow.runtime.db.commit()
        restored_before = len(host.hypervisor.restored)
        await asyncio.gather(
            flow.client.delete(f"/computers/{active}"), flow.runtime.reaper.cycle()
        )
        await flow.runtime.tasks.drain(timeout=5.0)
        assert len(host.hypervisor.restored) - restored_before == 1, (
            "one drain forked, the other found an empty queue"
        )
        assert sum(1 for _, cmd in host.guest.commands if cmd == "echo q") == 1
        cursor = await flow.runtime.db.execute("SELECT COUNT(*) FROM deferred_queue")
        assert (await cursor.fetchone()) == (0,)
