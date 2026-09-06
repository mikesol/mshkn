"""Exclusive restore: error_on_conflict is a 409, defer_on_conflict drains after destroy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mshkn.host import ExecResult

if TYPE_CHECKING:
    from .conftest import Flow


async def test_error_on_conflict_is_409_and_defer_drains_after_destroy(flow: Flow) -> None:
    host = flow.host
    host.guest.script["sync"] = ExecResult(0, "", "")
    host.guest.script["echo deferred"] = ExecResult(0, "deferred\n", "")
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (
        await flow.client.post(f"/computers/{base}/checkpoint", json={"label": "chain"})
    ).json()["checkpoint_id"]
    await flow.client.delete(f"/computers/{base}")
    first = await flow.client.post(
        f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"}
    )
    assert first.status_code == 200
    second = await flow.client.post(
        f"/checkpoints/{ckpt}/fork", json={"exclusive": "error_on_conflict"}
    )
    assert second.status_code == 409
    queued = await flow.client.post(
        f"/checkpoints/{ckpt}/fork",
        json={
            "exclusive": "defer_on_conflict",
            "exec": "echo deferred",
            "self_destruct": True,
            "callback_url": "http://receiver/cb",
        },
    )
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    resp = await flow.client.delete(f"/computers/{first.json()['computer_id']}")
    assert resp.status_code == 200
    await flow.runtime.tasks.drain(timeout=5.0)
    assert any(cmd == "echo deferred" for _, cmd in host.guest.commands)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 2, "the deferred run self-destructed into a second labelled checkpoint"
    assert flow.received and flow.received[0]["label"] == "chain"
    cur = await flow.runtime.db.execute("SELECT COUNT(*) FROM deferred_queue")
    assert (await cur.fetchone()) == (0,)
    assert host.hypervisor.alive == {}, "nothing left running after the drained self-destruct"
