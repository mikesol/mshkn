"""Exclusive restore: error_on_conflict is a 409, defer_on_conflict drains after destroy."""

from __future__ import annotations

import asyncio
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


async def _labelled_head(flow: Flow, label: str) -> str:
    """A destroyed base computer whose one checkpoint carries `label`; returns its id."""
    flow.host.guest.script["sync"] = ExecResult(0, "", "")
    base = (await flow.client.post("/computers", json={})).json()["computer_id"]
    ckpt = (await flow.client.post(f"/computers/{base}/checkpoint", json={"label": label})).json()[
        "checkpoint_id"
    ]
    await flow.client.delete(f"/computers/{base}")
    return str(ckpt)


async def test_concurrent_forks_by_label_with_error_on_conflict_advance_the_chain_once(
    flow: Flow,
) -> None:
    flow.host.guest.script["echo turn"] = ExecResult(0, "turn\n", "")
    head = await _labelled_head(flow, "chain")
    body = {
        "label": "chain",
        "exclusive": "error_on_conflict",
        "exec": "echo turn",
        "self_destruct": True,
    }

    first, second = await asyncio.gather(
        flow.client.post("/checkpoints/fork", json=body),
        flow.client.post("/checkpoints/fork", json=body),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    ok = first if first.status_code == 200 else second
    assert ok.json()["checkpoint_id"] == head
    assert ok.json()["exec_stdout"] == "turn\n"
    await flow.runtime.tasks.drain(timeout=5.0)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert [c["id"] for c in chain[1:]] == [head], "exactly one new head"
    assert chain[0]["parent_id"] == head
    assert chain[0]["id"] == ok.json()["created_checkpoint_id"]
    assert flow.host.hypervisor.alive == {}


async def test_concurrent_forks_by_label_with_defer_on_conflict_run_in_order(
    flow: Flow,
) -> None:
    flow.host.guest.script["echo turn"] = ExecResult(0, "turn\n", "")
    head = await _labelled_head(flow, "chain")
    body = {
        "label": "chain",
        "exclusive": "defer_on_conflict",
        "exec": "echo turn",
        "self_destruct": True,
    }

    first, second = await asyncio.gather(
        flow.client.post("/checkpoints/fork", json=body),
        flow.client.post("/checkpoints/fork", json=body),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 202]
    queued = first if first.status_code == 202 else second
    assert queued.json()["status"] == "queued"
    await flow.runtime.tasks.drain(timeout=5.0)
    chain = (await flow.client.get("/checkpoints", params={"label": "chain"})).json()
    assert len(chain) == 3, "the fork and the drained fork each self-destructed into a head"
    newest, middle, oldest = chain
    assert oldest["id"] == head
    assert middle["parent_id"] == head
    assert newest["parent_id"] == middle["id"]
    assert sum(1 for _, cmd in flow.host.guest.commands if cmd == "echo turn") == 2
    cur = await flow.runtime.db.execute("SELECT COUNT(*) FROM deferred_queue")
    assert (await cur.fetchone()) == (0,)
    assert flow.host.hypervisor.alive == {}


async def test_fork_by_an_unknown_label_is_404(flow: Flow) -> None:
    resp = await flow.client.post("/checkpoints/fork", json={"label": "nope"})
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


async def test_fork_by_label_is_scoped_to_the_account(flow: Flow) -> None:
    await _labelled_head(flow, "chain")
    resp = await flow.other_client.post("/checkpoints/fork", json={"label": "chain"})
    assert resp.status_code == 404
