"""The deferred queue's drain: a destroy and an idle reap racing on one label fork
exactly once, and a self-destructing fork that inherits its source label and
reports through its callback."""

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


async def test_fork_self_destruct_inherits_the_label_and_calls_back(flow: Flow) -> None:
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    host.guest.script["echo forked"] = ExecResult(0, "forked\n", "")
    cid = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{cid}/checkpoint", json={"label": "chain"})).json()[
        "checkpoint_id"
    ]
    resp = await flow.client.post(
        f"/checkpoints/{ckpt}/fork",
        json={
            "exec": "echo forked",
            "self_destruct": True,
            "callback_url": "http://receiver/cb",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_checkpoint_id"] and body["checkpoint_id"] == ckpt
    await flow.runtime.tasks.drain(timeout=2.0)
    assert flow.received == [
        {
            "computer_id": body["computer_id"],
            "checkpoint_id": ckpt,
            "label": "chain",
            "exec_exit_code": 0,
            "exec_stdout": "forked\n",
            "exec_stderr": "",
            "created_checkpoint_id": body["created_checkpoint_id"],
        }
    ]
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert {c["id"] for c in chain} == {ckpt, body["created_checkpoint_id"]}
    created = next(c for c in chain if c["id"] == body["created_checkpoint_id"])
    assert created["parent_id"] == ckpt
